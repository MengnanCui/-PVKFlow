"""CSV 文字格子防公式。

样品名、批次、实验名来自文件夹名 —— 是别人给的数据。Excel / WPS 打开 CSV 时，以这些字符开头的
文字格子会被当成公式执行（`=HYPERLINK(...)` 一类）。这种格子前面加一个 `'`，Excel 就当纯文字。
规则和前端「下载数据（CSV）」那份（web/js/csv.js）一样；批处理导出的 plot.py 读回时再去掉。
"""
from __future__ import annotations

import math

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def guard_formula(value):
    """文字格子防公式。数（包括 -0.5 这种负数）原样 —— 不然一列数就全变成文字了。"""
    if not isinstance(value, str) or not value.startswith(FORMULA_START):
        return value
    try:
        if math.isfinite(float(value)):
            return value
    except ValueError:
        pass
    return "'" + value
