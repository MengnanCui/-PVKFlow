"""按层级扫描 QFLS 数据根目录。**只读**：只列目录、只 stat，不打开、不改任何文件。

    根目录/
      实验文件夹/                       ← 根目录下的每个文件夹都可能是实验，也可能不是
        批次哈希目录/                   ← 实验下面一层；名字通常是十六进制哈希，不强求
          slot1/ slot2/ …               ← 数量不固定
            point1/ point2/ …           ← 数量不固定
              QFLS/0-20X-405R-1sun-时间戳-uuid.txt   ← 只取 0 开头的 txt；别的都是历史产物，跳过
              point1.qvdf                            ← 和点位同名的元数据

认不出层级的（备份、说明、别的实验类型、零散文件）列进 skipped 并写明原因 —— 不悄悄忽略。
根目录本身就是一个实验（下面直接是批次目录）也认。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.qfls import names

_HIDDEN = (".", "_", "~", "$")


def _ls(d: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(d) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _num(m) -> int:
    return int(m.group(1)) if m else 0


class _Scan:
    def __init__(self, root: Path, exclude: list[Path]):
        self.root = root
        self.exclude = [p.resolve() for p in exclude]
        self.skipped: list[dict] = []
        self.renamed: list[dict] = []
        self.frames: list[dict] = []
        self.experiments: list[dict] = []
        self.artifacts = 0
        self.artifact_examples: list[str] = []
        self._shown: dict[str, str] = {}

    # 显示用的相对路径：每一段都修过乱码
    def shown(self, p: Path) -> str:
        key = str(p)
        if key in self._shown:
            return self._shown[key]
        try:
            parts = p.relative_to(self.root).parts
        except ValueError:
            return key
        out = []
        cur = self.root
        for part in parts:
            cur = cur / part
            fixed, how = names.fix_name(part)
            if how and str(cur) not in {r["raw_path"] for r in self.renamed}:
                self.renamed.append({"raw_path": str(cur), "raw": part, "fixed": fixed, "how": how})
            out.append(fixed)
        self._shown[key] = "/".join(out)
        return self._shown[key]

    def skip(self, p: Path, kind: str, reason: str) -> None:
        self.skipped.append({"path": self.shown(p) or ".", "kind": kind, "reason": reason})

    def excluded(self, p: Path) -> bool:
        try:
            rp = p.resolve()
        except OSError:
            return False
        return any(rp == x or x in rp.parents or rp in x.parents for x in self.exclude)

    # ---------------------------------------------------------------- 层级
    @staticmethod
    def batches_of(exp: Path) -> list[Path]:
        """实验下面那一层里，含 slotN 子目录的就是批次目录。"""
        out = []
        for e in _ls(exp):
            if e.is_dir() and any(c.is_dir() and names.SLOT_RE.match(c.name) for c in _ls(Path(e.path))):
                out.append(Path(e.path))
        return out

    def run(self) -> dict:
        if self.batches_of(self.root):
            self.experiment(self.root, top=True)
        else:
            for e in _ls(self.root):
                p = Path(e.path)
                if e.is_file():
                    self.skip(p, "file", "根目录下的零散文件，不属于任何实验")
                    continue
                if not e.is_dir():
                    continue
                if self.excluded(p):
                    self.skip(p, "dir", "这是平台自己的输出目录，不扫")
                    continue
                if e.name.startswith(_HIDDEN):
                    self.skip(p, "dir", "隐藏 / 临时目录")
                    continue
                if not self.batches_of(p):
                    self.skip(p, "dir", self.why_not_experiment(p))
                    continue
                self.experiment(p)
        return {
            "root": str(self.root),
            "experiments": self.experiments,
            "frames": self.frames,
            "skipped": self.skipped,
            "renamed": self.renamed,
            "artifacts": self.artifacts,
            "artifact_examples": self.artifact_examples[:20],
        }

    def why_not_experiment(self, p: Path) -> str:
        kids = _ls(p)
        if not kids:
            return "空目录"
        if any(c.is_dir() and names.SLOT_RE.match(c.name) for c in kids):
            return "slot 目录直接放在这一层，缺了批次哈希目录那一层"
        if any(c.is_file() and names.is_frame_file(c.name) for c in kids):
            return "直接放着 QFLS 数据文件，但没有 批次 / slot / point 层级"
        return "下面没有「批次目录 / slotN / pointN」结构 —— 不是 QFLS 实验（可能是备份、说明或别的实验类型）"

    def experiment(self, exp: Path, top: bool = False) -> None:
        display = names.fix_name(exp.name)[0]
        if not top:
            self.shown(exp)          # 顺便把乱码修复记一笔
        info = {"name": display, "raw_name": exp.name, "path": self.shown(exp) if not top else ".",
                "batches": [], "n_frames": 0, "n_points": 0}
        batch_dirs = set(self.batches_of(exp))
        for e in _ls(exp):
            p = Path(e.path)
            if p in batch_dirs:
                continue
            self.skip(p, "dir" if e.is_dir() else "file",
                      "实验目录下不是批次目录（下面没有 slotN）" if e.is_dir() else "实验目录下的零散文件")
        for b in sorted(batch_dirs):
            info["batches"].append(self.batch(display, b))
        info["n_frames"] = sum(x["n_frames"] for x in info["batches"])
        info["n_points"] = sum(x["n_points"] for x in info["batches"])
        self.experiments.append(info)

    def batch(self, exp_name: str, b: Path) -> dict:
        bname = names.fix_name(b.name)[0]
        info = {"name": bname, "looks_like_hash": bool(names.HASH_RE.match(bname)),
                "n_slots": 0, "n_points": 0, "n_frames": 0}
        slots = []
        for e in _ls(b):
            p = Path(e.path)
            m = names.SLOT_RE.match(e.name) if e.is_dir() else None
            if m:
                slots.append((_num(m), p))
            else:
                self.skip(p, "dir" if e.is_dir() else "file", "批次目录下不是 slotN")
        for sno, s in sorted(slots):
            info["n_slots"] += 1
            points = []
            for e in _ls(s):
                p = Path(e.path)
                m = names.POINT_RE.match(e.name) if e.is_dir() else None
                if m:
                    points.append((_num(m), p))
                else:
                    self.skip(p, "dir" if e.is_dir() else "file", "slot 目录下不是 pointN")
            for pno, pt in sorted(points):
                info["n_points"] += 1
                info["n_frames"] += self.point(exp_name, bname, sno, pno, pt)
        return info

    def point(self, exp: str, batch: str, sno: int, pno: int, pt: Path) -> int:
        kids = _ls(pt)
        qfls = next((Path(k.path) for k in kids if k.is_dir() and k.name.lower() == "qfls"), None)
        qvdf = next((Path(k.path) for k in kids if k.is_file()
                     and k.name.lower() == f"{pt.name.lower()}.qvdf"), None)
        if qvdf is None:   # 名字没完全对上的 .qvdf 也认，但记下来
            qvdf = next((Path(k.path) for k in kids if k.is_file() and k.name.lower().endswith(".qvdf")), None)
        if qfls is None:
            self.skip(pt, "dir", "点位下没有 QFLS 子目录")
            return 0
        found = []
        for e in _ls(qfls):
            if e.is_file() and names.is_frame_file(e.name):
                found.append(Path(e.path))
            else:
                self.artifacts += 1
                if len(self.artifact_examples) < 20:
                    self.artifact_examples.append(self.shown(Path(e.path)))
        for i, f in enumerate(found):
            parsed = names.parse_frame_name(f.name)
            rel = self.shown(f)
            key = "q" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
            self.frames.append({
                "key": key, "experiment": exp, "batch": batch, "slot": f"slot{sno}", "point": f"point{pno}",
                "slot_no": sno, "point_no": pno, "file": f.name, "rel": rel, "path": str(f),
                "size_bytes": f.stat().st_size,
                "qvdf": str(qvdf) if qvdf else None, "qvdf_rel": self.shown(qvdf) if qvdf else None,
                "qvdf_name_ok": bool(qvdf and qvdf.name.lower() == f"{pt.name.lower()}.qvdf"),
                "n_in_point": len(found), "index_in_point": i + 1, **parsed,
            })
        if not found:
            self.skip(qfls, "dir", "QFLS 目录里没有 0 开头的 txt")
        return len(found)


def scan(root: str | Path, exclude: list[Path] | None = None) -> dict:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"不是一个文件夹：{root}")
    out = _Scan(root, exclude or []).run()
    out["frames"].sort(key=lambda f: (f["experiment"], f["batch"], f["slot_no"], f["point_no"],
                                      f["objective"] or "", f["timestamp"] or "", f["file"]))
    return out
