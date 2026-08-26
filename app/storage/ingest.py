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
    """文件修改时间，ISO 8601。文件夹名里没有时间戳时的退路。"""
    from datetime import datetime, timezone

    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


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
        abs_path = entry.get("abs_path") or entry.get("path")
        if not abs_path:
            report.failed.append({"path": None, "error": "缺少 abs_path"})
            continue
        src = Path(abs_path)
        try:
            if not src.is_file():
                raise FileNotFoundError("文件不存在或不是普通文件")

            display = entry.get("display_path") or src.name
            ext = (entry.get("ext") or src.suffix).lower()
            mode = entry.get("storage_mode") or classify(ext, copy_ext, ref_ext, unknown)

            sha = sha256_file(src)
            dup = db.query_one(
                "SELECT artifact_id, filename FROM artifact WHERE sha256 = ? AND kind = 'raw'",
                (sha,),
            )
            if dup:
                report.duplicates.append({
                    "path": str(src), "display_path": display,
                    "artifact_id": dup["artifact_id"], "existing": dup["filename"],
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
                if not target.exists():
                    shutil.copy2(src, target)
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

            _mark_matrix(aid, src, ext)

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
            if not target.exists():
                target.write_bytes(data)

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


def verify_references() -> dict:
    """巡检引用型文件是否还在原位。断链要显式暴露，不能等用户跑分析时才炸。"""
    rows = db.query(
        "SELECT artifact_id, original_path, status FROM artifact"
        " WHERE storage_mode='referenced' AND kind='raw'"
    )
    missing, restored = [], []
    for r in rows:
        p = r["original_path"]
        alive = bool(p) and Path(p).is_file()
        want = "ok" if alive else "missing"
        if want != r["status"]:
            with db.tx() as c:
                c.execute("UPDATE artifact SET status=? WHERE artifact_id=?", (want, r["artifact_id"]))
            (restored if alive else missing).append(r["artifact_id"])
        elif not alive:
            missing.append(r["artifact_id"])
    return {"checked": len(rows), "missing": missing, "restored": restored}


def _mark_matrix(artifact_id: str, path: Path, ext: str) -> None:
    """导入时就判定这个文件是不是光谱矩阵。

    以前这件事是惰性的 —— 只有当有人访问 /api/spectra/samples 时才回填。
    在那之前，筛选式的 has_matrix 和批处理看到的是「零个矩阵」，
    刚导完就去跑批处理会命中 0 个样品，而且没有任何提示。
    """
    fmt = ""
    try:
        if ext in {".xlsx", ".xls", ".xlsm"}:
            flag, cols = True, None
        else:
            from app.parsers import insitu_csv, sniff

            if insitu_csv.looks_like_insitu(path):
                flag, cols, fmt = True, None, "insitu_data_csv"
            else:
                sn = sniff.sniff_text(path, max_lines=40)   # 抬头可能有十几行
                cols = len(sn.columns)
                flag = bool(sn.ok and cols >= 8)
    except Exception:
        return          # 判不出来就留 NULL，让惰性那条路以后再补

    with db.tx() as c:
        row = c.execute("SELECT meta_json FROM artifact WHERE artifact_id=?",
                        (artifact_id,)).fetchone()
        try:
            meta = json.loads((row["meta_json"] if row else None) or "{}")
        except json.JSONDecodeError:
            meta = {}
        meta["matrix_like"] = flag
        meta["columns_hint"] = cols
        if fmt:
            meta["source_format"] = fmt
        c.execute("UPDATE artifact SET is_matrix=?, meta_json=? WHERE artifact_id=?",
                  (1 if flag else 0, json.dumps(meta, ensure_ascii=False), artifact_id))
