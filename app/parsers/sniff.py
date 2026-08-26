"""文本数据文件的嗅探：编码、分隔符、表头在第几行、每列是什么类型。

仪器导出的文件普遍有几行说明性抬头，然后才是真表头。pandas 直接读会翻车，
所以先嗅探再读——这是让"任意文件导入后都不是死的"的前提。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEXT_EXT = {".csv", ".txt", ".dat", ".tsv", ".asc", ".log", ".spc", ".xy"}
EXCEL_EXT = {".xlsx", ".xls", ".xlsm"}

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "latin-1")
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


@dataclass
class Sniffed:
    encoding: str = "utf-8"
    delimiter: str = ","
    header_row: int | None = 0     # 表头所在行（0-based，已跳过抬头）
    skip_rows: int = 0             # 表头之前要跳过的行数
    columns: list[str] = field(default_factory=list)
    dtypes: dict[str, str] = field(default_factory=dict)   # numeric | text
    preamble: list[str] = field(default_factory=list)      # 抬头原文，可能含仪器参数
    n_data_rows_sampled: int = 0
    ok: bool = True
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "encoding": self.encoding, "delimiter": self.delimiter,
            "header_row": self.header_row, "skip_rows": self.skip_rows,
            "columns": self.columns, "dtypes": self.dtypes,
            "preamble": self.preamble[:20], "ok": self.ok, "reason": self.reason,
        }


def read_text(path: Path, max_bytes: int = 256_000) -> tuple[str, str]:
    """按候选编码依次尝试，返回 (文本, 编码)。"""
    raw = Path(path).open("rb").read(max_bytes)
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def _guess_delimiter(lines: list[str]) -> str:
    """按候选分隔符切分，看哪个能让多行的列数保持一致且大于 1。"""
    candidates = [",", "\t", ";", "|", " "]
    best, best_score = ",", -1.0
    for d in candidates:
        counts = []
        for ln in lines:
            if not ln.strip():
                continue
            parts = ln.split() if d == " " else ln.split(d)
            counts.append(len(parts))
        if not counts:
            continue
        common = max(set(counts), key=counts.count)
        if common < 2:
            continue
        consistency = counts.count(common) / len(counts)
        score = consistency * min(common, 12)
        if score > best_score:
            best, best_score = d, score
    return best


def _is_number(tok: str) -> bool:
    t = tok.strip().strip('"').strip("'")
    if not t or t.lower() in ("nan", "inf", "-inf", "na", "null", "none", "-", "--"):
        return False
    return bool(_NUM.match(t))


def _split(line: str, delim: str) -> list[str]:
    if delim == " ":
        return line.split()
    try:
        return next(csv.reader(io.StringIO(line), delimiter=delim))
    except (csv.Error, StopIteration):
        return line.split(delim)


def sniff_text(path: Path, max_lines: int = 200) -> Sniffed:
    """找出抬头有多少行、表头在哪、每列什么类型。"""
    text, enc = read_text(Path(path))
    lines = text.splitlines()[:max_lines]
    if not lines:
        return Sniffed(encoding=enc, ok=False, reason="文件为空")

    delim = _guess_delimiter(lines)

    # 从下往上找：第一行"全是数字"的行，它上面那行就是表头
    numeric_rows: list[int] = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        cells = _split(ln, delim)
        if len(cells) < 2:
            continue
        numeric = sum(1 for c in cells if _is_number(c))
        if numeric >= max(2, int(len(cells) * 0.6)):
            numeric_rows.append(i)

    if not numeric_rows:
        return Sniffed(encoding=enc, delimiter=delim, header_row=None, ok=False,
                       reason="没有识别到数值数据行，可能不是表格文件")

    first_data = numeric_rows[0]
    header_row = None
    for j in range(first_data - 1, -1, -1):
        if lines[j].strip():
            cells = _split(lines[j], delim)
            # 表头行：列数对得上，且大部分不是数字
            if len(cells) >= 2 and sum(1 for c in cells if _is_number(c)) <= len(cells) // 2:
                header_row = j
            break

    preamble = [ln for ln in lines[: (header_row if header_row is not None else first_data)]
                if ln.strip()]

    if header_row is not None:
        columns = [c.strip().strip('"') or f"col{i+1}"
                   for i, c in enumerate(_split(lines[header_row], delim))]
    else:
        n = len(_split(lines[first_data], delim))
        columns = [f"col{i+1}" for i in range(n)]

    # 采样数据行判断列类型
    sample = [_split(ln, delim) for ln in lines[first_data:first_data + 50] if ln.strip()]
    dtypes: dict[str, str] = {}
    for i, name in enumerate(columns):
        vals = [row[i] for row in sample if i < len(row)]
        if vals and sum(1 for v in vals if _is_number(v)) >= len(vals) * 0.8:
            dtypes[name] = "numeric"
        else:
            dtypes[name] = "text"

    return Sniffed(
        encoding=enc, delimiter=delim,
        header_row=header_row, skip_rows=(header_row if header_row is not None else first_data),
        columns=columns, dtypes=dtypes, preamble=preamble,
        n_data_rows_sampled=len(sample),
    )


def load_frame(path: Path, sniffed: Sniffed | None = None, max_rows: int | None = None):
    """按嗅探结果把文件读成 DataFrame。Excel 走 openpyxl。"""
    import pandas as pd

    p = Path(path)
    if p.suffix.lower() in EXCEL_EXT:
        df = pd.read_excel(p, nrows=max_rows)
        return df, Sniffed(encoding="binary", delimiter="", columns=[str(c) for c in df.columns],
                           dtypes={str(c): ("numeric" if pd.api.types.is_numeric_dtype(df[c])
                                            else "text") for c in df.columns})

    s = sniffed or sniff_text(p)
    if not s.ok:
        raise ValueError(s.reason)

    kwargs: dict[str, Any] = {
        "encoding": s.encoding,
        "nrows": max_rows,
        "engine": "python",          # 容忍不规整的行
        "on_bad_lines": "skip",
    }
    if s.delimiter == " ":
        kwargs["sep"] = r"\s+"
    else:
        kwargs["sep"] = s.delimiter

    if s.header_row is not None:
        kwargs["skiprows"] = s.header_row
        kwargs["header"] = 0
    else:
        kwargs["skiprows"] = s.skip_rows
        kwargs["header"] = None

    df = pd.read_csv(p, **kwargs)
    if s.header_row is None:
        df.columns = [f"col{i+1}" for i in range(df.shape[1])]
    df.columns = [str(c).strip() for c in df.columns]
    return df, s
