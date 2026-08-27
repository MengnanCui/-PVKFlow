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

import re
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


# 时间轴那一行怎么认。**不做精确字符串比较** —— 真实文件里这一行可能是
# 半角括号、全角括号、多个空格，或者换成英文。栽在这上面的话，
# 平台不会报错，只是静默拿不到时间轴。
_TIME_RE = re.compile(r"(相对.*时间|relative.*time|elapsed)", re.I)
_CLOCK_RE = re.compile(r"(采集时间|^时间$|timestamp|clock)", re.I)

# 一行里至少这么多格，才算「数据行」而不是抬头的 key/value
_MIN_DATA_CELLS = 4


def _cells(line: str, sep: str = "\t") -> list[str]:
    """切成单元格，并把右侧补齐用的空列去掉。

    仪器把每一行都补到同样的列数，抬头那几行后面因此跟着上百个空分隔符。
    """
    parts = line.rstrip("\r\n").split(sep)
    while parts and parts[-1].strip() == "":
        parts.pop()
    return parts


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _guess_sep(lines: list[str]) -> str:
    """分隔符按「哪个能把最长的那些行切出最多格」来定。

    原来这里写死了 \t。真实文件万一是逗号或分号分隔，写死的结果不是报错，
    而是每一行都只有一格 —— 于是一个块都找不到，报「不像光谱矩阵」。
    """
    best, best_n = "\t", 0
    for sep in ("\t", ",", ";", "|"):
        n = max((len(ln.split(sep)) for ln in lines[:400]), default=0)
        if n > best_n:
            best, best_n = sep, n
    return best


def _read_lines(path: Path) -> list[str]:
    """整个文件读进来。真实文件约 2 MB，一次读完比逐行 IO 快。

    编码交给 sniff.decode_bytes —— 它会先认 UTF-16，再退回 UTF-8 / GB18030。
    """
    from app.parsers.sniff import decode_bytes

    text, _ = decode_bytes(Path(path).read_bytes())
    return text.splitlines()


def outline(path: str | Path, max_lines: int = 25) -> str:
    """文件结构速写：每行的首格 + 有几格。

    解析失败时把它放进报错里。没有这个的话，用户看到的只有「不像光谱矩阵」，
    我这边也无从判断到底是分隔符、编码，还是块名对不上 ——
    错误信息本身就该是排查通道。
    """
    try:
        lines = _read_lines(Path(path))
    except OSError as exc:
        return f"（读不出来：{exc}）"
    sep = _guess_sep(lines)
    name = {"\t": "TAB", ",": "逗号", ";": "分号", "|": "竖线"}.get(sep, repr(sep))
    out = [f"分隔符看起来是 {name}，共 {len(lines)} 行。前 {max_lines} 行的结构："]
    for i, ln in enumerate(lines[:max_lines]):
        c = _cells(ln, sep)
        head = (c[0].strip()[:38] if c else "")
        out.append(f"  {i + 1:>4}  {len(c):>4} 格  首格={head!r}")
    return "\n".join(out)


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
    sep = _guess_sep(lines)

    header: dict[str, Any] = {}
    blocks: dict[str, dict] = {}
    current: dict | None = None

    for line in lines:
        cells = _cells(line, sep)
        if not cells:
            continue
        head = cells[0].strip().lstrip("\ufeff")

        # 块起点：一个**非数字**的首格 + 很多格。
        # 原来这里要求首格字面上以 "Wavelength" 结尾 —— 那是把一次样本
        # 当成了格式定义。真实文件里这一格可能是 "Absorption"（Wavelength
        # 被分隔符切到了下一格）、大小写不同、或者中英混排。
        # 认结构不认名字：抬头是 2 格，数据块的头是几百格。
        if len(cells) > _MIN_DATA_CELLS and _to_float(head) is None \
                and not _TIME_RE.search(head) and not _CLOCK_RE.search(head):
            name = _block_name(head, cells)
            current = {"name": name, "n_cols": len(cells) - 1,
                       "t": None, "clock": None, "lam": [], "rows": []}
            blocks[name] = current
            continue

        if current is None:
            # 还没进任何块 —— 这里是 key<分隔符>value 的抬头。
            # 白名单之外的键也收下：不同版本的固件抬头字段不一样，
            # 丢掉的话报告里就回显不出来了。
            if len(cells) >= 2 and head:
                v = _to_float(cells[1])
                header[head] = cells[1].strip() if v is None else v
            continue

        if _TIME_RE.search(head):
            current["t"] = [_to_float(c) for c in cells[1:]]
            continue
        if _CLOCK_RE.search(head):
            current["clock"] = [c.strip() for c in cells[1:]]
            continue

        lam = _to_float(head)
        if lam is None:
            continue
        current["lam"].append(lam)
        current["rows"].append(cells[1:])

    # 从这里往下，出错一律把文件结构附上 —— 我这边光看「不像光谱矩阵」
    # 是没法判断到底是分隔符、编码还是块名对不上的。
    if not blocks:
        raise InsituFormatError(
            f"{p.name} 里没有找到任何数据块（首格非数字、后面跟着几百格的那种行）。\n\n"
            + outline(p))

    picked = _pick_block(blocks, block)
    if picked is None:
        raise InsituFormatError(
            f"{p.name} 里没有 {block} 块，只有：{'、'.join(sorted(blocks))}。"
            f"（不会拿 Origin 块顶替 —— 那是原始 PD 计数，不是吸收谱。）\n\n"
            + outline(p))

    b = blocks[picked]
    block = picked
    if b["t"] is None:
        # 没有「相对第一帧时间」这一行时，可以**从采集时间推**——那是文件里
        # 真有的信息。但两样都没有就必须报错，不能拿帧序号编一条充数：
        # 下游的横轴写着「时间 (s)」，编出来的轴会让每一张膜厚曲线都在撒谎。
        b["t"] = _time_from_clock(b["clock"]) if b["clock"] else None
        if b["t"] is None:
            raise InsituFormatError(
                f"{p.name} 的 {block} 块里找不到时间轴 —— 既没有「{TIME_ROW}」"
                f"这样的相对时间行，也没有能换算的「{CLOCK_ROW}」。\n\n" + outline(p))
        header["_time_axis"] = f"由「{CLOCK_ROW}」推算（文件里没有相对时间行）"

    if len(b["lam"]) < 4:
        raise InsituFormatError(
            f"{p.name} 的 {block} 块只有 {len(b['lam'])} 条波长，太少了。\n\n" + outline(p))

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


def _block_name(head: str, cells: list[str]) -> str:
    """块名。首格通常是「Absorption Wavelength」，但也可能只是「Absorption」，
    「Wavelength」被分隔符切到了第二格。两种都归一到同一个名字。"""
    name = head
    if name.lower().endswith("wavelength"):
        name = name[: -len("wavelength")].strip()
    elif len(cells) > 1 and cells[1].strip().lower() == "wavelength":
        pass                                  # 首格本身就是块名
    return name.strip(" \t:：") or "?"


def _pick_block(blocks: dict, want: str) -> str | None:
    """按名字挑块，宽进严出：先精确、再忽略大小写、再子串。

    只有一个块时直接用它 —— 有些固件不写 Origin 块，此时唯一的那块
    就是吸收谱，为了名字大小写不同而报错没有道理。
    """
    if want in blocks:
        return want
    low = {k.lower(): k for k in blocks}
    if want.lower() in low:
        return low[want.lower()]
    hits = [k for k in blocks if want.lower() in k.lower()]
    if len(hits) == 1:
        return hits[0]
    # 只有一个块时可以用它 —— **但绝不能是已知的另一个块**。
    # Origin 是原始 PD 计数，量纲和量级都跟吸收谱不一样；拿它顶替的话
    # 界面上一切正常，算出来的东西全是错的。宽容到这一步就要停。
    if len(blocks) == 1:
        only = next(iter(blocks))
        if only.lower() != ORIGIN.lower():
            return only
    return None


def _time_from_clock(clock: list[str]) -> list[float] | None:
    """采集时间（32:50.3 这种 分:秒.毫秒）→ 相对秒。跨分钟要累加。"""
    secs: list[float] = []
    for c in clock:
        parts = c.strip().split(":")
        try:
            if len(parts) == 2:
                secs.append(int(parts[0]) * 60 + float(parts[1]))
            elif len(parts) == 3:
                secs.append(int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]))
            else:
                return None
        except ValueError:
            return None
    if len(secs) < 2:
        return None
    # 跨小时/跨分钟会回绕，补上整周期
    out, bump = [], 0.0
    for i, v in enumerate(secs):
        if i and v + bump < out[-1]:
            bump += 3600.0
        out.append(v + bump)
    return [v - out[0] for v in out]


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


def looks_like_insitu(path: str | Path, max_lines: int = 400) -> bool:
    """便宜地判断：这是不是原位 Data.csv。

    **认结构，不认名字。** 上一版靠首格字面上以 "Wavelength" 结尾、或者
    等于 "Mode"/"相对第一帧时间(s)" 来判断 —— 那是把手上这一份样本
    当成了格式定义。真实文件里块名可能被分隔符切开、大小写不同、
    括号是全角的，任意一条对不上，整个文件就悄悄退回普通宽表那条路，
    最后报一句「不像光谱矩阵：只有 9 行 × 2 列」（那 9 行正是抬头）。

    真正稳定的特征只有一条：**先是一串两格的 key/value 抬头，
    然后出现首格非数字、后面跟着几十上百格的块头，再往下是首格为波长的数据行。**
    """
    p = Path(path)
    if p.suffix.lower() not in (".csv", ".txt", ".tsv", ".dat"):
        return False
    try:
        lines = _read_lines(p)[:max_lines]
    except OSError:
        return False
    if not lines:
        return False

    sep = _guess_sep(lines)
    saw_block_head = False
    saw_data_after = False
    saw_time_row = False
    kv_preamble = 0

    for line in lines:
        cells = _cells(line, sep)
        if not cells:
            continue
        head = cells[0].strip().lstrip("\ufeff")

        if len(cells) == 2 and not saw_block_head and _to_float(head) is None:
            kv_preamble += 1               # 抬头那一串 key/value
            continue
        if len(cells) <= _MIN_DATA_CELLS:
            continue

        if _TIME_RE.search(head) or _CLOCK_RE.search(head):
            saw_time_row = True            # 块内的时间/采集时间行
        elif _to_float(head) is None:
            saw_block_head = True          # 块头：非数字首格 + 很多格
        elif saw_block_head:
            saw_data_after = True          # 块头之后的波长行

    # 光有「块头 + 数据行」还不够 —— **普通宽表长得一模一样**
    #（一行表头 + 一堆数值行）。原位格式独有的是那两样之一：
    # 块内的时间/采集时间行，或者块之前那一串两格的 key/value 抬头。
    # 少了这一条，所有普通光谱宽表都会被误判成原位格式。
    return saw_block_head and saw_data_after and (saw_time_row or kv_preamble >= 2)


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
