"""光谱矩阵：波长 × 时间的二维数据。

in-situ 旋涂监测的原始形态就是这个 —— 一列一个时刻的光谱。
它和普通两列表格差别足够大，值得单独一个解析器。

解析一个 5.7 MB 的 CSV 要 270 ms，解析结果缓存成 npz 后读回只要 5 ms。
所以这里做了内容寻址的磁盘缓存：同一个文件只会被真正解析一次。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app import config

# 波长的合理范围（nm）。用来判断矩阵是哪个方向摆的。
LAMBDA_MIN, LAMBDA_MAX = 180.0, 3000.0

_META_LINE = re.compile(r"^\s*[#%]\s*([A-Za-z][\w .-]*)\s*[:=]\s*(.+?)\s*$")


@dataclass
class SpectralMatrix:
    lam: np.ndarray                 # (L,) nm，升序
    t: np.ndarray                   # (T,) s，升序
    M: np.ndarray                   # (L, T) float32
    meta: dict[str, Any] = field(default_factory=dict)
    orientation: str = "wavelength_rows"

    @property
    def shape(self) -> tuple[int, int]:
        return self.M.shape

    def describe(self) -> dict:
        L, T = self.M.shape
        d_lam = float(np.median(np.diff(self.lam))) if L > 1 else 0.0
        d_t = float(np.median(np.diff(self.t))) if T > 1 else 0.0
        finite = np.isfinite(self.M)
        return {
            "n_lambda": int(L),
            "n_time": int(T),
            "lambda_min": float(self.lam[0]),
            "lambda_max": float(self.lam[-1]),
            "lambda_step": d_lam,
            "time_min": float(self.t[0]),
            "time_max": float(self.t[-1]),
            "time_step": d_t,
            "frame_rate_hz": (1.0 / d_t) if d_t > 0 else 0.0,
            "value_min": float(np.nanmin(self.M)) if finite.any() else 0.0,
            "value_max": float(np.nanmax(self.M)) if finite.any() else 0.0,
            "bytes": int(self.M.nbytes),
            "orientation": self.orientation,
            "meta": self.meta,
        }


# ------------------------------------------------------------------ 判向
def _looks_like_wavelength(v: np.ndarray) -> float:
    """给一个轴打分：它有多像波长轴。0–1。"""
    if v.size < 16:
        return 0.0
    finite = v[np.isfinite(v)]
    if finite.size < 16:
        return 0.0
    in_range = float(np.mean((finite >= LAMBDA_MIN) & (finite <= LAMBDA_MAX)))
    d = np.diff(finite)
    monotonic = float(max(np.mean(d > 0), np.mean(d < 0)))
    # 波长轴通常是等间隔的
    even = 0.0
    if d.size and np.abs(np.median(d)) > 0:
        even = float(np.mean(np.abs(d / np.median(d) - 1) < 0.05))
    return 0.55 * in_range + 0.25 * monotonic + 0.20 * even


def _parse_preamble(path: Path, max_lines: int = 60) -> tuple[dict[str, Any], int]:
    """读文件头的注释行。仪器参数经常藏在那儿。返回 (元数据, 注释行数)。"""
    from app.parsers.sniff import read_text

    text, _ = read_text(path, max_bytes=16_000)
    meta: dict[str, Any] = {}
    n = 0
    for line in text.splitlines():
        if not line.strip():
            n += 1
            continue
        if not line.lstrip().startswith(("#", "%")):
            break
        n += 1
        m = _META_LINE.match(line)
        if m:
            key, value = m.group(1).strip(), m.group(2).strip()
            try:
                meta[key] = float(value) if re.fullmatch(r"[-+]?[\d.eE+-]+", value) else value
            except ValueError:
                meta[key] = value
    return meta, n


def parse(path: str | Path) -> SpectralMatrix:
    """把一个宽表文件解析成光谱矩阵。两个方向都认。"""
    import pandas as pd

    p = Path(path)
    meta, _ = _parse_preamble(p)

    if p.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        df = pd.read_excel(p)
    else:
        from app.parsers.sniff import sniff_text

        s = sniff_text(p)
        sep = r"\s+" if s.delimiter == " " else s.delimiter
        df = pd.read_csv(p, comment="#", sep=sep, engine="python",
                         encoding=s.encoding, on_bad_lines="skip")

    if df.shape[1] < 4 or df.shape[0] < 4:
        raise ValueError(f"不像光谱矩阵：只有 {df.shape[0]} 行 × {df.shape[1]} 列")

    first_col = pd.to_numeric(df.iloc[:, 0], errors="coerce").to_numpy(np.float64)
    header = pd.to_numeric(pd.Series(df.columns[1:]), errors="coerce").to_numpy(np.float64)

    score_col = _looks_like_wavelength(first_col)
    score_hdr = _looks_like_wavelength(header)

    if score_col >= score_hdr and score_col > 0.5:
        orientation = "wavelength_rows"
        lam = first_col
        t = header
        M = df.iloc[:, 1:].to_numpy(np.float32)
    elif score_hdr > 0.5:
        orientation = "time_rows"
        lam = header
        t = first_col
        M = df.iloc[:, 1:].to_numpy(np.float32).T      # → (L, T)
    else:
        raise ValueError(
            "认不出哪个轴是波长。首列打分 "
            f"{score_col:.2f}、表头打分 {score_hdr:.2f}，都低于 0.5。\n"
            f"首列范围 {np.nanmin(first_col):.4g}–{np.nanmax(first_col):.4g}，"
            f"表头范围 {np.nanmin(header):.4g}–{np.nanmax(header):.4g}。\n"
            "期望其中一个是 180–3000 nm 的单调等间隔序列。"
        )

    if np.isnan(t).all():
        t = np.arange(M.shape[1], dtype=np.float64)     # 表头不是时间就退回帧序号
        meta["_time_axis"] = "帧序号（表头不是数字）"

    ok_lam = np.isfinite(lam)
    lam, M = lam[ok_lam], M[ok_lam]
    ok_t = np.isfinite(t)
    t, M = t[ok_t], M[:, ok_t]

    if lam.size > 1 and lam[0] > lam[-1]:               # 统一成升序
        lam, M = lam[::-1], M[::-1]
    if t.size > 1 and t[0] > t[-1]:
        t, M = t[::-1], M[:, ::-1]

    return SpectralMatrix(lam=lam.astype(np.float64), t=t.astype(np.float64),
                          M=np.ascontiguousarray(M), meta=meta, orientation=orientation)


# ------------------------------------------------------------------ 缓存
def _cache_path(sha256: str) -> Path:
    return config.WORKSPACE / "cache" / "matrix" / f"{sha256}.npz"


def load_cached(path: str | Path, sha256: str | None = None) -> SpectralMatrix:
    """解析并缓存。同一个文件只会被真正解析一次（270ms → 5ms）。"""
    p = Path(path)
    if sha256 is None:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        sha256 = h.hexdigest()

    cache = _cache_path(sha256)
    if cache.is_file():
        try:
            z = np.load(cache, allow_pickle=True)
            return SpectralMatrix(
                lam=z["lam"], t=z["t"], M=z["M"],
                meta=dict(z["meta"].item()) if "meta" in z else {},
                orientation=str(z["orientation"]) if "orientation" in z else "wavelength_rows",
            )
        except Exception:
            cache.unlink(missing_ok=True)      # 缓存坏了就重来，不要卡住用户

    sm = parse(p)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".tmp")
    # 注意：np.savez 会给不以 .npz 结尾的路径自动补后缀，所以必须传文件对象
    with open(tmp, "wb") as fh:
        np.savez(fh, lam=sm.lam, t=sm.t, M=sm.M,
                 meta=np.array(sm.meta, dtype=object), orientation=sm.orientation)
    tmp.replace(cache)                          # 原子替换，避免半截缓存
    return sm


def looks_like_matrix(path: str | Path) -> bool:
    """便宜的预判：只读文件头，不整个解析。"""
    p = Path(path)
    if p.suffix.lower() not in {".csv", ".txt", ".dat", ".tsv", ".asc",
                                ".xlsx", ".xls", ".xlsm"}:
        return False
    if p.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        return True
    try:
        from app.parsers.sniff import sniff_text
        s = sniff_text(p, max_lines=8)
        return bool(s.ok and len(s.columns) >= 8)
    except Exception:
        return False
