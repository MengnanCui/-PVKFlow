"""原位吸收光谱仪的 Data.csv —— 真实的仪器输出格式。

一个主文件夹下若干子文件夹，每个子文件夹是一次测量：

    ZG0013_2026072918354709_Mode5_202607291932_SPS100/
        Data.csv                  ← 这个文件
        Heatmap_Absorption.png
        Options.json
        PostProcessState.json

Data.csv 是**制表符分隔**的，长这样：

    Mode                     Mode5                       ← 抬头，key<TAB>value
    CollectionDuration(s)    120
    ...
    PDRemainingRatio         33.5
                                                          ← 空行
    Origin Wavelength        <GUID> <GUID> ...            ← 第一块：原始 PD 计数
    采集时间                  32:50.3  32:50.4  ...
    相对第一帧时间(s)          0  0.082  0.182  ...        ← 时间轴在这儿，不在表头
    322.036                  1036  1031  ...
    ...
    Absorption Wavelength    <GUID> <GUID> ...            ← 第二块：这才是要的
    采集时间                  32:50.3  ...
    相对第一帧时间(s)          0  0.082  ...
    330.276                  0  100  90.909  ...

两个块的波长网格**不一样**（Origin 从 322.036 起，Absorption 从 330.276 起），
所以不能只解析一遍再切 —— 必须分块各读各的轴。

为什么不用 matrix.py：那个假设第一行是表头、抬头行以 # 开头、整个文件一块数据。
这里三条全不成立。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.parsers.matrix import SpectralMatrix
from app.parsers.sniff import read_text

# 块名。文件里写作 "<块名> Wavelength"。
ABSORPTION = "Absorption"
ORIGIN = "Origin"

TIME_ROW = "相对第一帧时间(s)"
CLOCK_ROW = "采集时间"

# 抬头里这些字段是光强标定与时序参数。冻结规范 §5 块 D 点名说了它们**不含几何信息**，
# 所以入射角仍然未知 —— 带上来是为了让报告能如实回显，不是为了算什么。
_HEADER_KEYS = {
    "Mode", "CollectionDuration(s)", "TaskDuration(s)",
    "MeasurementBrightPD", "MeasurementDarkPD",
    "ReferencePDDiff", "ReferencePDRatio",
    "PDDeltaFromReference", "PDRemainingRatio",
}


class InsituFormatError(ValueError):
    """这个文件不是原位光谱的 Data.csv，或者缺了必需的块。"""


def _cells(line: str) -> list[str]:
    """切成单元格，并把右侧补齐用的空列去掉。

    仪器把每一行都补到同样的列数，抬头那几行后面因此跟着上百个空 tab。
    """
    parts = line.rstrip("\r\n").split("\t")
    while parts and parts[-1].strip() == "":
        parts.pop()
    return parts


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _read_lines(path: Path) -> list[str]:
    """整个文件读进来。真实文件约 2 MB，一次读完比逐行 IO 快。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace").splitlines()


def parse(path: str | Path, block: str = ABSORPTION) -> SpectralMatrix:
    """读一个 Data.csv。

    默认取 Absorption 块 —— Origin 是原始 PD 计数，只在排查仪器问题时才看。

    **关于 Absorption 的物理量**：这一列是 0–100 的百分比，不是吸光度
    A = -log10(T)。百分比对 T 是线性的，正弦变换后还是同频率的正弦，
    FFT 峰位不动；只有 -log10 那种非线性才会生成 2f/3f 谐波假峰
    （冻结规范 §2 与 STEP 0）。所以下游按 input_is_absorbance=False 处理。
    真拿到吸光度输入时，把那个开关打开即可，接口留着。
    """
    p = Path(path)
    lines = _read_lines(p)

    header: dict[str, Any] = {}
    blocks: dict[str, dict] = {}
    current: dict | None = None

    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        head = cells[0].strip()

        # 块起点：<块名> Wavelength
        if head.endswith("Wavelength") and len(cells) > 4:
            name = head[: -len("Wavelength")].strip() or "?"
            current = {"name": name, "n_cols": len(cells) - 1,
                       "t": None, "clock": None, "lam": [], "rows": []}
            blocks[name] = current
            continue

        if current is None:
            # 还没进任何块 —— 这里是 key<TAB>value 的抬头
            if len(cells) >= 2 and head in _HEADER_KEYS:
                v = _to_float(cells[1])
                header[head] = cells[1].strip() if v is None else v
            continue

        if head == TIME_ROW:
            current["t"] = [_to_float(c) for c in cells[1:]]
            continue
        if head == CLOCK_ROW:
            current["clock"] = [c.strip() for c in cells[1:]]
            continue

        lam = _to_float(head)
        if lam is None:
            continue
        current["lam"].append(lam)
        current["rows"].append(cells[1:])

    if not blocks:
        raise InsituFormatError(
            f"{p.name} 里没有找到任何数据块。"
            f"原位 Data.csv 应该有一行以「Absorption Wavelength」开头。")

    if block not in blocks:
        raise InsituFormatError(
            f"{p.name} 里没有 {block} 块，只有：{'、'.join(sorted(blocks))}。"
            f"（不会拿 Origin 块顶替 —— 那是原始 PD 计数，不是吸收谱。）")

    b = blocks[block]
    if b["t"] is None:
        raise InsituFormatError(
            f"{p.name} 的 {block} 块里没有「{TIME_ROW}」这一行，时间轴无从谈起。")
    if len(b["lam"]) < 4:
        raise InsituFormatError(
            f"{p.name} 的 {block} 块只有 {len(b['lam'])} 条波长，太少了。")

    lam, t, M = _assemble(b)

    meta: dict[str, Any] = dict(header)
    meta["block"] = block
    meta["blocks_present"] = sorted(blocks)
    meta["value_kind"] = "absorption_percent" if block == ABSORPTION else "pd_counts"
    meta["input_is_absorbance"] = False      # 见上：百分比是线性的，不是 -log10(T)
    if b["clock"]:
        meta["clock_first"] = b["clock"][0]
        meta["clock_last"] = b["clock"][-1]
    meta["source_format"] = "insitu_data_csv"

    return SpectralMatrix(lam=lam, t=t, M=M, meta=meta, orientation="wavelength_rows")


def _assemble(b: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把一个块的行拼成 (lam, t, M)，并把坏轴丢掉。

    数据行的列数可能比时间轴短（仪器写文件时被打断），按最短的对齐 ——
    补 NaN 会让下游的 FFT 见到不存在的采样点。
    """
    t_raw = b["t"] or []
    n_t = len(t_raw)
    for row in b["rows"]:
        n_t = min(n_t, len(row))
    if n_t < 2:
        raise InsituFormatError(f"{b['name']} 块的时间轴只有 {n_t} 帧。")

    lam = np.asarray(b["lam"], dtype=np.float64)
    t = np.asarray([np.nan if v is None else v for v in t_raw[:n_t]], dtype=np.float64)

    M = np.empty((len(lam), n_t), dtype=np.float32)
    for i, row in enumerate(b["rows"]):
        M[i] = [np.nan if (v := _to_float(c)) is None else v for c in row[:n_t]]

    # 非有限的轴丢掉；两个轴都排成升序
    keep_l = np.isfinite(lam)
    keep_t = np.isfinite(t)
    lam, M = lam[keep_l], M[keep_l][:, keep_t]
    t = t[keep_t]
    if lam.size and lam[0] > lam[-1]:
        lam, M = lam[::-1], M[::-1]
    if t.size and t[0] > t[-1]:
        t, M = t[::-1], M[:, ::-1]
    return lam, t, np.ascontiguousarray(M)


def looks_like_insitu(path: str | Path, max_lines: int = 40) -> bool:
    """便宜地判断：这是不是原位 Data.csv。

    只读前 max_lines 行 —— Absorption 块在一千多行以后，等不到它，
    但抬头的 Mode 和第一个 Origin Wavelength 就在开头。
    """
    p = Path(path)
    if p.suffix.lower() not in (".csv", ".txt", ".tsv", ".dat"):
        return False
    try:
        text, _ = read_text(p, max_bytes=64_000)
    except OSError:
        return False
    for line in text.splitlines()[:max_lines]:
        head = line.split("\t", 1)[0].strip()
        if head.endswith("Wavelength") and "\t" in line:
            return True
        if head in ("Mode", TIME_ROW) and "\t" in line:
            return True
    return False


def block_names(path: str | Path) -> list[str]:
    """文件里有哪几块。诊断用。"""
    out = []
    for line in _read_lines(Path(path)):
        head = line.split("\t", 1)[0].strip()
        if head.endswith("Wavelength") and "\t" in line:
            name = head[: -len("Wavelength")].strip()
            if name and name not in out:
                out.append(name)
    return out
