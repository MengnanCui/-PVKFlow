"""读一帧 QFLS 矩阵并校验；读点位的 qvdf 元数据。只读。

txt：UTF-8（带 BOM）、逗号分隔、无表头的浮点矩阵，单位 eV。行列数随采集配置变（现在是 1216 × 1824，约 24 MB）。
校验三件事，任何一件不过这一帧就**不打分**（指标照实标「无效」，不拿坏数据凑一个分）：
  · 行列一致：每一行的列数都一样（少一个逗号就是一行错位）
  · 无缺失：没有空字段、NaN、inf、认不出的字符
  · 范围合理：超出 PARAMS["valid_range_ev"] 的像素占比 ≤ PARAMS["invalid_out_of_range_frac"] 才算可用，
    少量超范围的按中位数填补后再算空间指标，并标出来
"""
from __future__ import annotations

import codecs
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

from app.util.text import decode_text


class FrameError(Exception):
    """这一帧不能用。message 是给人看的原因。"""


def read_matrix(path: str | Path) -> tuple[np.ndarray, dict]:
    raw = Path(path).read_bytes()
    info = {"bom": raw.startswith(codecs.BOM_UTF8), "bytes": len(raw)}
    if info["bom"]:
        raw = raw[len(codecs.BOM_UTF8):]
    if not raw.strip():
        raise FrameError("文件是空的")
    if not raw.isascii():
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise FrameError("不是 UTF-8 文本（可能是二进制或别的编码）") from None
        raise FrameError("里面有数字以外的字符（中文、全角符号等）")
    text = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").rstrip(b"\n")
    lines = text.split(b"\n")
    ncols = np.fromiter((ln.count(b",") + 1 for ln in lines), dtype=np.int64, count=len(lines))
    vals, cnt = np.unique(ncols, return_counts=True)
    if len(vals) > 1:
        main = int(vals[np.argmax(cnt)])
        bad = np.flatnonzero(ncols != main)
        eg = "、".join(f"第 {i + 1} 行 {ncols[i]} 列" for i in bad[:3])
        raise FrameError(f"行列不一致：大多数行 {main} 列，有 {len(bad)} 行不是（{eg}）")
    if text.startswith(b",") or b",\n" in text or b"\n," in text or b",," in text or text.endswith(b","):
        n_empty = text.count(b",,") + text.count(b",\n") + text.count(b"\n,")
        raise FrameError(f"有缺失值：至少 {max(n_empty, 1)} 个空字段")
    try:
        arr = pd.read_csv(io.BytesIO(text), header=None, dtype=np.float64, engine="c",
                          na_filter=True).to_numpy()
    except (ValueError, pd.errors.ParserError) as exc:
        raise FrameError(f"有认不出的数值：{str(exc)[:160]}") from None
    if arr.ndim != 2 or arr.shape[0] < 4 or arr.shape[1] < 4:
        raise FrameError(f"矩阵太小：{arr.shape[0]} 行 × {arr.shape[1] if arr.ndim == 2 else 1} 列")
    bad = ~np.isfinite(arr)
    if bad.any():
        raise FrameError(f"有缺失值：{int(bad.sum())} 个 NaN / inf")
    info["rows"], info["cols"] = arr.shape
    return arr, info


def check_range(arr: np.ndarray, lo: float, hi: float, max_frac: float) -> tuple[np.ndarray, dict]:
    """超范围的像素：占比太大 → 整帧无效；少量 → 按中位数填补（只为了空间指标能算），并报出来。"""
    out = (arr < lo) | (arr > hi)
    n = int(out.sum())
    frac = n / arr.size
    rep = {"out_of_range_px": n, "out_of_range_frac": frac,
           "min": float(arr.min()), "max": float(arr.max())}
    if frac > max_frac:
        raise FrameError(f"范围不合理：{frac:.1%} 的像素在 {lo}–{hi} eV 以外（最小 {arr.min():.3g}、最大 {arr.max():.3g} eV）")
    if n:
        arr = arr.copy()
        arr[out] = float(np.median(arr[~out]))
    return arr, rep


# ------------------------------------------------------------------ qvdf
# qvdf 的格式没有拿到说明（推断）：JSON、XML、INI / key=value 都认；认出来的键都留着
_ALIASES = {
    "objective": ("objective", "magnification", "mag", "物镜", "objective_lens"),
    "laser": ("laser", "excitation", "激光"),
    "wavelength_nm": ("wavelength_nm", "wavelength", "波长"),
    "intensity": ("intensity", "light_intensity", "光强", "power"),
    "frames": ("frames", "frame_count", "n_frames", "帧数"),
    "pixel_size_um": ("pixel_size_um", "pixel_um", "pixelsize", "pixel_size", "像素尺寸"),
    "exposure_ms": ("exposure_ms", "exposure", "曝光"),
    "timestamp": ("timestamp", "time", "datetime", "采集时间"),
    "stage_x_um": ("stage_x_um", "stage_x", "x_um"),
    "stage_y_um": ("stage_y_um", "stage_y", "y_um"),
}


def _decode(b: bytes) -> str:
    return decode_text(b)          # 全平台一份编码判断（app/util/text.py）：UTF-16 / UTF-8 / GB18030


def _flatten(d, prefix="") -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}{k}." if isinstance(v, dict) else f"{prefix}{k}"))
    else:
        out[prefix.rstrip(".")] = d
    return out


def read_qvdf(path: str | Path | None) -> dict:
    if not path:
        return {"found": False, "raw": {}, "fields": {}}
    try:
        text = _decode(Path(path).read_bytes()[:1_000_000])
    except OSError as exc:
        return {"found": False, "error": str(exc), "raw": {}, "fields": {}}
    raw: dict[str, str] = {}
    fmt = "text"
    s = text.strip()
    if s.startswith("{"):
        try:
            raw = {k: str(v) for k, v in _flatten(json.loads(s)).items()}
            fmt = "json"
        except json.JSONDecodeError:
            raw = {}
    elif s.startswith("<"):
        try:
            for el in ET.fromstring(s).iter():
                if el.text and el.text.strip() and not list(el):
                    raw[el.tag] = el.text.strip()
                for k, v in el.attrib.items():
                    raw[f"{el.tag}.{k}"] = v
            fmt = "xml"
        except ET.ParseError:
            raw = {}
    if not raw:
        section = ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";", "//")):
                continue
            m = re.fullmatch(r"\[(.+)\]", line)
            if m:
                section = m.group(1).strip() + "."
                continue
            m = re.match(r"([^=:\t]+?)\s*[=:\t]\s*(.*)$", line)
            if m:
                raw[section + m.group(1).strip()] = m.group(2).strip()
        fmt = "ini" if raw else "unknown"
    low = {k.lower().rsplit(".", 1)[-1]: v for k, v in raw.items()}
    fields = {}
    for name, keys in _ALIASES.items():
        for k in keys:
            if k.lower() in low:
                fields[name] = low[k.lower()]
                break
    for k in ("pixel_size_um", "wavelength_nm", "exposure_ms", "stage_x_um", "stage_y_um"):
        if k in fields:
            try:
                fields[k] = float(re.sub(r"[^\d.eE+-]", "", fields[k]))
            except ValueError:
                fields.pop(k)
    if "frames" in fields:
        try:
            fields["frames"] = int(float(fields["frames"]))
        except ValueError:
            fields.pop("frames")
    if "objective" in fields:     # 一个点位用过几个物镜就可能写几个：「20X,100X」
        objs = [f"{x}X" for x in re.findall(r"(\d+(?:\.\d+)?)\s*[xX×]?", str(fields["objective"]))]
        fields["objectives"] = objs
        fields["objective"] = objs[0] if len(objs) == 1 else ",".join(objs)
    per_obj = {}                  # pixel_size_um_20X = …：按物镜分开写的像素尺寸
    for k, v in low.items():
        m = re.fullmatch(r"pixel_size_um[_ ]?(\d+(?:\.\d+)?)x", k)
        if m:
            try:
                per_obj[f"{m.group(1)}X"] = float(v)
            except ValueError:
                pass
    if per_obj:
        fields["pixel_size_um_by_objective"] = per_obj
    return {"found": True, "format": fmt, "raw": dict(list(raw.items())[:80]), "fields": fields}
