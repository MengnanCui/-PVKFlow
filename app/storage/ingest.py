"""导入：把磁盘上的实验文件登记进工作区。

分类落地策略（用户指定）：
  * 文本类（csv/txt/dat/json/xlsx…）—— 复制进 workspace/raw/，按 sha256 内容寻址。
    这类文件小、易丢、经常被人手改，复制一份才谈得上可复现。
  * 图像类（png/jpg/tif…）—— **不复制**，只登记绝对路径 + sha256 + 尺寸，
    另外生成一张缩略图供界面预览。图像动辄几百 MB，搬一遍不划算。

两条铁律：
  1. 原始文件永不被修改。复制是只读复制，引用是只读引用。
  2. 引用型文件找不到时状态标 missing，不静默失败。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from app import config
from app.storage import db, naming

_CHUNK = 1 << 20  # 1 MiB


# ------------------------------------------------------------------ 基础工具
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _settings() -> tuple[set[str], set[str], str, list[str]]:
    d = config.DEFAULTS
    copy_ext = {e.lower() for e in db.get_setting("copy_extensions", list(d.copy_extensions))}
    ref_ext = {e.lower() for e in db.get_setting("reference_extensions", list(d.reference_extensions))}
    unknown = db.get_setting("unknown_policy", d.unknown_policy)
    rules = db.get_setting("naming_rules", list(d.naming_rules))
    return copy_ext, ref_ext, unknown, list(rules)


def classify(ext: str, copy_ext: set[str], ref_ext: set[str], unknown: str) -> str:
    """返回 'copied' 或 'referenced'。"""
    e = ext.lower()
    if e in copy_ext:
        return "copied"
    if e in ref_ext:
        return "referenced"
    return "copied" if unknown == "copy" else "referenced"


def raw_target(sha: str, ext: str) -> Path:
    """内容寻址：同样的内容永远落在同一个位置，天然去重。"""
    return config.RAW_DIR / sha[:2] / f"{sha}{ext.lower()}"


def rel_to_workspace(p: Path) -> str:
    try:
        return p.resolve().relative_to(config.WORKSPACE).as_posix()
    except ValueError:
        return p.as_posix()


# ------------------------------------------------------------------ 扫描
@dataclass(frozen=True)
class ScannedFile:
    abs_path: str
    display_path: str   # 相对扫描根的路径，用于命名规则与界面展示
    size: int
    ext: str


_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".idea", ".vscode", "$RECYCLE.BIN"}


def _mtime_iso(path: Path) -> str:
    """文件修改时间，ISO 8601。文件夹名里没有时间戳时的退路。

    写成**本地时间、不带时区**，和文件夹名里解析出来的时间（仪器写的本地时间）同一种写法。
    原来写的是 UTC（带 +00:00）：按时间筛选是字符串比较，两种写法混在一起，
    没有时间戳的那几个在中国就偏 8 小时（自查 §6 #25）。已经存成 UTC 的由第 2 步迁移改过来。
    """
    from datetime import datetime

    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def scan(root: str | Path, recursive: bool = True) -> list[ScannedFile]:
    """扫描一个目录或单个文件，返回可导入的文件清单。不写任何东西。"""
    root = Path(root).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"路径不存在：{root}")

    if root.is_file():
        st = root.stat()
        return [ScannedFile(str(root.resolve()), root.name, st.st_size, root.suffix)]

    out: list[ScannedFile] = []
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = p.relative_to(base).as_posix()
            out.append(ScannedFile(str(p), rel, size, p.suffix))
        if not recursive:
            break
    return out


def _store_copy(target: Path, expected_size: int, write) -> None:
    """把一份原始文件落进 workspace/raw/。**要么完整写进去，要么不留痕。**

    原来是「目标不存在就直接往目标写」：导入到一半断电或被杀，留下半截文件；
    下次导入看见「已经存在」就跳过，从此这份截断的数据一直被当成正常的（自查 §6 #14）。

    现在先写到同目录的临时文件，写完再原子地换名过去；
    已经存在的也要核一下大小 —— 对不上就是上次留下的半截，重写。
    （不重算 sha：那要把文件再读一遍，而截断一定会让大小对不上。）
    """
    if target.exists():
        try:
            if target.stat().st_size == expected_size:
                return
        except OSError:
            pass
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _measurement_key(display_path: str) -> str | None:
    """这个文件能不能凭文件夹名认出「是哪一次测量」。能就返回用来比对的键。

    仪器的子文件夹名形如 ZG0013_2026072918354709_Mode5_…，里面带着测量时刻，
    一次测量一个名字 —— 撞名就是同一次测量（重新导出、换地方放都算）。
    认不出时间戳的文件夹名（sample1、B12、随手起的名字）不承担这个身份，
    返回 None，只按内容和绝对路径去重。
    """
    parts = display_path.split("/")
    if len(parts) != 2:
        return None
    return display_path if naming.parse_run_folder(parts[0]).measured_at else None


# 原位测量一个子文件夹一次，数据文件固定叫这个
RUN_DATA_FILE = "Data.csv"


def scan_folders(root: str | Path, data_file: str = RUN_DATA_FILE) -> dict:
    """按子文件夹扫描：主文件夹 → 每个含 Data.csv 的子文件夹出一条。

    为什么单独一条路径，而不是改命名规则：默认规则里的 `{sample}` 会把
    每个 Data.csv 都命名成 "Data"，所有测量挤成同一个样品。改全局规则
    又会打断已有的按文件名导入。所以做成导入时的**模式选择**。

    样品名 = 完整文件夹名。同一片样品测两次就是两个文件夹、两个样品 ——
    只按前缀 ZG0014 认身份的话，两次测量会被静默合并成一次。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        raise NotADirectoryError(f"按子文件夹导入需要一个目录：{root}")

    base = root.resolve()
    rows = []
    skipped = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        target = child / data_file
        if not target.is_file():
            skipped.append({"folder": child.name, "reason": f"没有 {data_file}"})
            continue
        info = naming.parse_run_folder(child.name)
        try:
            size = target.stat().st_size
        except OSError as exc:
            skipped.append({"folder": child.name, "reason": str(exc)})
            continue
        rows.append({
            "abs_path": str(target),
            "display_path": f"{child.name}/{data_file}",
            "filename": data_file,
            "ext": target.suffix.lower(),
            "size": size,
            "storage_mode": "copied",
            "sample": info.name,          # 完整文件夹名
            "batch": info.device,         # ZG0013 —— 界面上叫「样品号」
            "method": "absorbance",
            "measured_at": info.measured_at,
            "mode": info.mode,
            "rule": "@run-folder",
            "matched": True,
        })

    return {
        "root": str(base),
        "mode": "folders",
        "count": len(rows),
        "matched": len(rows),
        "to_copy": len(rows),
        "to_reference": 0,
        "files": rows,
        "skipped": skipped,
    }


def scan_preview(root: str | Path, recursive: bool = True) -> dict:
    """扫描 + 分类 + 样品匹配预览。导入前给人看，确认后才写库。"""
    files = scan(root, recursive)
    copy_ext, ref_ext, unknown, rules = _settings()
    rows = []
    for f in files:
        m = naming.parse(f.display_path, rules)
        rows.append({
            "abs_path": f.abs_path,
            "display_path": f.display_path,
            "filename": Path(f.abs_path).name,
            "ext": f.ext.lower(),
            "size": f.size,
            "storage_mode": classify(f.ext, copy_ext, ref_ext, unknown),
            "sample": m.sample,
            "batch": m.batch,
            "method": m.method,
            "rule": m.rule,
            "matched": m.matched,
        })
    return {
        "root": str(Path(root).expanduser().resolve()),
        "mode": "files",
        "count": len(rows),
        "matched": sum(1 for r in rows if r["matched"]),
        "to_copy": sum(1 for r in rows if r["storage_mode"] == "copied"),
        "to_reference": sum(1 for r in rows if r["storage_mode"] == "referenced"),
        "files": rows,
    }


# ------------------------------------------------------------------ 导入
@dataclass
class IngestReport:
    batch_id: str
    imported: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    samples_created: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "samples_created": self.samples_created,
            "counts": {
                "imported": len(self.imported),
                "duplicates": len(self.duplicates),
                "failed": len(self.failed),
            },
        }


def _ensure_sample(sample_name: str, batch: str) -> str:
    """样品的身份是 (名字, 批次)，不是只看名字。

    命名规则会把 B20_S1 解析成 batch=B20 / sample=S1，于是 S1 这个名字在
    每个批次里都会出现一次。只按名字查重的话，12 个批次的 S1 会被静默合并成
    一个样品 —— 数据没丢，但全串了，而且小数据集上根本看不出来。
    """
    if not sample_name:
        return ""
    batch = (batch or "").strip()
    existing = db.query_one(
        "SELECT sample_id FROM sample WHERE name = ? AND COALESCE(batch,'') = ?",
        (sample_name, batch))
    if existing:
        return existing["sample_id"]
    sid = db.new_id("smp")
    with db.tx() as c:
        c.execute(
            "INSERT INTO sample(sample_id, name, batch, created_at) VALUES(?,?,?,?)",
            (sid, sample_name, batch or None, db.now()),
        )
    return sid


def _make_thumbnail(src: Path, sha: str) -> tuple[str | None, dict]:
    """图像不复制，但生成缩略图，界面才能预览。失败不影响导入。"""
    meta: dict = {}
    try:
        from PIL import Image
    except ImportError:
        return None, meta
    try:
        with Image.open(src) as im:
            meta["width"], meta["height"] = im.width, im.height
            meta["mode"] = im.mode
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            im.thumbnail((config.DEFAULTS.thumbnail_max_px,) * 2)
            out = config.DERIVED_DIR / "thumbs" / f"{sha}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            im.save(out, "JPEG", quality=82)
        return rel_to_workspace(out), meta
    except Exception:
        return None, meta


def ingest_paths(
    entries: Sequence[dict],
    source_hint: str = "",
    batch_id: str | None = None,
) -> IngestReport:
    """把 scan_preview 里（可能被用户改过的）行写进库。

    每行至少要有 abs_path；display_path / sample / batch / method 可选，
    没有就现场用命名规则补。
    """
    config.ensure_dirs()
    copy_ext, ref_ext, unknown, rules = _settings()

    bid = batch_id or db.new_id("bat")
    with db.tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO import_batch(batch_id, source_hint, file_count, created_at) "
            "VALUES(?,?,?,?)",
            (bid, source_hint or None, 0, db.now()),
        )

    report = IngestReport(batch_id=bid)
    seen_samples: set[str] = set()

    for entry in entries:
        # 一串裸路径也接着 —— 脚本和 curl 里最自然的写法就是给一个字符串列表，
        # 为此丢一个 500 出去说不过去（栈回溯不是给用户看的诊断信息）
        if isinstance(entry, str):
            entry = {"abs_path": entry}
        elif not isinstance(entry, dict):
            report.failed.append({"path": None, "error": f"不认识的条目：{type(entry).__name__}"})
            continue
        abs_path = entry.get("abs_path") or entry.get("path")
        if not abs_path:
            report.failed.append({"path": None, "error": "缺少 abs_path"})
            continue
        src = Path(abs_path)
        try:
            if not src.is_file():
                raise FileNotFoundError("文件不存在或不是普通文件")

            # 没给 display_path 时补上父文件夹。**不能只用文件名**：
            # 三个样品文件夹里都躺着一个 Data.csv，只拿 src.name 的话
            # 下面那条「同一个文件夹」会把它们判成同一份，三个样品静默变一个。
            # 这类错在小数据集上看不出来 —— 数字对不上时已经分析了半天了。
            display = entry.get("display_path") or (
                f"{src.parent.name}/{src.name}" if src.parent.name else src.name)
            ext = (entry.get("ext") or src.suffix).lower()
            mode = entry.get("storage_mode") or classify(ext, copy_ext, ref_ext, unknown)

            # ── 去重，三条依次判，都会记进 duplicates 并说清是哪一条命中的 ──
            #
            # ① 内容相同（sha256）：同一份数据换个地方放，不该进两次
            # ② 路径相同：同一个文件再导一次
            # ③ **同一次测量的子文件夹**：仪器重新导出一次，字节会变、sha 会变、
            #    甚至换了个主文件夹放，但那还是同一次测量。
            #    **只在文件夹名里带着仪器时间戳时才判**（见 _measurement_key）——
            #    那样的名字一次测量一个，撞名就是同一次。原来对所有文件都按相对路径比，
            #    两个主文件夹下各有一个 film.csv、内容不同，第二个被当成重复静默丢掉
            #    （自查 §6 #3）。
            sha = sha256_file(src)
            abs_str = str(src.resolve())
            mkey = _measurement_key(display)
            dup = db.query_one(
                "SELECT artifact_id, filename, display_path,"
                "       CASE WHEN sha256 = ? THEN '内容相同'"
                "            WHEN original_path = ? THEN '同一个文件'"
                "            ELSE '同一次测量的文件夹' END AS why"
                "  FROM artifact"
                " WHERE kind = 'raw'"
                "   AND (sha256 = ? OR original_path = ?"
                "        OR (? IS NOT NULL AND display_path = ?))"
                " LIMIT 1",
                (sha, abs_str, sha, abs_str, mkey, mkey),
            )
            if dup:
                report.duplicates.append({
                    "path": str(src), "display_path": display,
                    "artifact_id": dup["artifact_id"], "existing": dup["filename"],
                    "reason": dup["why"],
                })
                continue

            # 样品归属：优先用调用方给的（用户在预览页改过的），否则按规则解析
            sample_name = (entry.get("sample") or "").strip()
            batch_name = (entry.get("batch") or "").strip()
            method = (entry.get("method") or "").strip()
            if not sample_name:
                m = naming.parse(display, rules)
                sample_name, batch_name, method = m.sample, m.batch or batch_name, m.method or method
            sample_id = _ensure_sample(sample_name, batch_name)
            if sample_id:
                seen_samples.add(f"{batch_name}/{sample_name}" if batch_name else sample_name)

            stored_path = None
            thumb_path = None
            meta: dict = {}

            if mode == "copied":
                target = raw_target(sha, ext)
                target.parent.mkdir(parents=True, exist_ok=True)
                _store_copy(target, src.stat().st_size, lambda tmp, src=src: shutil.copy2(src, tmp))
                stored_path = rel_to_workspace(target)
            else:
                # 引用型：原图不动，只做缩略图（图像才有）
                if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}:
                    thumb_path, meta = _make_thumbnail(src, sha)

            aid = db.new_id("art")
            with db.tx() as c:
                c.execute(
                    "INSERT INTO artifact(artifact_id, kind, storage_mode, sha256, original_path,"
                    " display_path, stored_path, filename, ext, mime, size, status, thumb_path,"
                    " meta_json, sample_id, batch_id, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid, "raw", mode, sha, str(src.resolve()), display, stored_path,
                        src.name, ext, mimetypes.guess_type(src.name)[0],
                        src.stat().st_size, "ok", thumb_path,
                        json.dumps(meta, ensure_ascii=False),
                        sample_id or None, bid, db.now(),
                    ),
                )
                if method and sample_id:
                    # measured_at 是「按时间筛选」的唯一来源。文件夹名里解析不出来时
                    # 退回文件的修改时间 —— 有个近似的时间，比这一维直接失效强。
                    measured_at = (entry.get("measured_at") or "").strip()
                    if not measured_at:
                        measured_at = _mtime_iso(src)
                    c.execute(
                        "INSERT INTO measurement(measurement_id, sample_id, method,"
                        " measured_at, created_at)"
                        " SELECT ?,?,?,?,? WHERE NOT EXISTS("
                        "   SELECT 1 FROM measurement WHERE sample_id=? AND method=?)",
                        (db.new_id("mea"), sample_id, method, measured_at, db.now(),
                         sample_id, method),
                    )

            mark_matrix(aid, src)

            report.imported.append({
                "artifact_id": aid, "filename": src.name, "display_path": display,
                "storage_mode": mode, "sample": sample_name, "size": src.stat().st_size,
            })
        except Exception as exc:
            report.failed.append({"path": str(src), "error": str(exc)})

    report.samples_created = sorted(seen_samples)
    with db.tx() as c:
        c.execute(
            "UPDATE import_batch SET file_count = "
            "(SELECT COUNT(*) FROM artifact WHERE batch_id = ?) WHERE batch_id = ?",
            (bid, bid),
        )
    return report


def ingest_uploads(uploads: Iterable[tuple[str, bytes]], source_hint: str = "") -> IngestReport:
    """浏览器拖拽上传的文件。

    注意：浏览器出于安全不会给出原始路径，所以**这条通道一律复制**，
    图像也不例外。想让图像走"原地引用"，必须用服务端路径导入。
    """
    config.ensure_dirs()
    _, _, _, rules = _settings()
    bid = db.new_id("bat")
    with db.tx() as c:
        c.execute(
            "INSERT INTO import_batch(batch_id, source_hint, file_count, created_at) VALUES(?,?,?,?)",
            (bid, source_hint or "浏览器上传", 0, db.now()),
        )

    report = IngestReport(batch_id=bid)
    seen: set[str] = set()
    for display, data in uploads:
        try:
            name = Path(display).name
            ext = Path(name).suffix.lower()
            sha = sha256_bytes(data)
            dup = db.query_one(
                "SELECT artifact_id, filename FROM artifact WHERE sha256=? AND kind='raw'", (sha,)
            )
            if dup:
                report.duplicates.append({
                    "path": display, "display_path": display,
                    "artifact_id": dup["artifact_id"], "existing": dup["filename"],
                })
                continue

            target = raw_target(sha, ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            _store_copy(target, len(data), lambda tmp, data=data: tmp.write_bytes(data))

            m = naming.parse(display, rules)
            sample_id = _ensure_sample(m.sample, m.batch)
            if m.sample:
                seen.add(m.sample)

            thumb_path, meta = (None, {})
            if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}:
                thumb_path, meta = _make_thumbnail(target, sha)

            aid = db.new_id("art")
            with db.tx() as c:
                c.execute(
                    "INSERT INTO artifact(artifact_id, kind, storage_mode, sha256, original_path,"
                    " display_path, stored_path, filename, ext, mime, size, status, thumb_path,"
                    " meta_json, sample_id, batch_id, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid, "raw", "copied", sha, None, display, rel_to_workspace(target),
                        name, ext, mimetypes.guess_type(name)[0], len(data), "ok", thumb_path,
                        json.dumps(meta, ensure_ascii=False),
                        sample_id or None, bid, db.now(),
                    ),
                )
            report.imported.append({
                "artifact_id": aid, "filename": name, "display_path": display,
                "storage_mode": "copied", "sample": m.sample, "size": len(data),
            })
        except Exception as exc:
            report.failed.append({"path": display, "error": str(exc)})

    report.samples_created = sorted(seen)
    with db.tx() as c:
        c.execute(
            "UPDATE import_batch SET file_count=(SELECT COUNT(*) FROM artifact WHERE batch_id=?)"
            " WHERE batch_id=?", (bid, bid),
        )
    return report


def verify_references(limit: int = 200) -> dict:
    """巡检原始文件还在不在。断链要显式暴露，不能等用户跑分析时才炸。

    两种都查：引用型（只登记了路径，原文件在你的盘上 —— 最容易被挪走）和复制型
    （在工作区 raw/ 里，一般不会丢，但有人手动清过工作区的话也会断）。状态变了的写回库
    （missing ↔ ok），并把**找不到的是哪些**列出来：文件名、记录的路径、属于哪个样品 ——
    光给一个「3 个找不到」，人还得自己去翻是哪三个。列表最多 `limit` 条，总数照实给。
    """
    t0 = time.perf_counter()
    rows = db.query(
        """SELECT a.artifact_id, a.storage_mode, a.original_path, a.stored_path, a.display_path,
                  a.filename, a.status, a.sample_id, s.name AS sample_name, s.batch
             FROM artifact a LEFT JOIN sample s ON s.sample_id = a.sample_id
            WHERE a.kind = 'raw' ORDER BY a.created_at, a.artifact_id""")
    missing, restored, details, changes = [], [], [], []
    n_ref = 0
    for r in rows:
        if r["storage_mode"] == "copied" and r["stored_path"]:
            path = config.WORKSPACE / r["stored_path"]
        else:
            n_ref += 1
            path = Path(r["original_path"]) if r["original_path"] else None
        alive = bool(path) and path.is_file()
        want = "ok" if alive else "missing"
        if want != r["status"]:
            changes.append((want, r["artifact_id"]))
            if alive:
                restored.append(r["artifact_id"])
        if not alive:
            missing.append(r["artifact_id"])
            if len(details) < limit:
                details.append({
                    "artifact_id": r["artifact_id"], "filename": r["filename"],
                    "display_path": r["display_path"], "path": str(path) if path else "",
                    "storage_mode": r["storage_mode"], "sample_id": r["sample_id"],
                    "sample_name": r["sample_name"], "batch": r["batch"],
                    "newly_missing": r["status"] != "missing",
                })
    if changes:
        with db.tx() as c:
            c.executemany("UPDATE artifact SET status = ? WHERE artifact_id = ?", changes)
    return {
        "checked": len(rows), "checked_referenced": n_ref, "checked_copied": len(rows) - n_ref,
        "missing": missing, "restored": restored, "missing_files": details,
        "truncated": len(missing) > len(details), "ms": round((time.perf_counter() - t0) * 1000),
    }


def mark_matrix(artifact_id: str, path: Path, *, if_unknown: bool | None = None):
    """判断这个文件是不是光谱矩阵（matrix.probe），结果写进 artifact。返回那个 Probe。

    导入时就判（原来是惰性的 —— 只有当有人访问 /api/spectra/samples 时才回填。
    在那之前，筛选式的 has_matrix 和批处理看到的是「零个矩阵」，
    刚导完就去跑批处理会命中 0 个样品，而且没有任何提示）。
    列样品时碰到还没判过的老数据，也走这里补 —— 两条路用的是同一份判断。

    判不出来时：`if_unknown` 为 None 就不写（留 NULL，以后再补）；给了值就按它写。
    """
    from app.parsers import matrix

    pr = matrix.probe(path)
    flag = pr.is_matrix if pr.is_matrix is not None else if_unknown
    if flag is None:
        return pr

    with db.tx() as c:
        row = c.execute("SELECT meta_json FROM artifact WHERE artifact_id=?",
                        (artifact_id,)).fetchone()
        try:
            meta = json.loads((row["meta_json"] if row else None) or "{}")
        except json.JSONDecodeError:
            meta = {}
        meta["matrix_like"] = flag
        meta["columns_hint"] = pr.columns
        if pr.source_format:
            meta["source_format"] = pr.source_format
        # is_matrix 是真列，筛选走它；meta_json 里那份留着给界面看细节
        c.execute("UPDATE artifact SET is_matrix=?, meta_json=? WHERE artifact_id=?",
                  (1 if flag else 0, json.dumps(meta, ensure_ascii=False), artifact_id))
    return pr
