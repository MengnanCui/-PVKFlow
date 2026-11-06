"""前端「下载数据（CSV）」的拼法（web/js/csv.js），在 node 里跑。

自查 §6 #29：曲线名来自文件夹名和样品名 —— 是别人给的数据。以 `=` 开头的名字，
Excel 打开下载的 CSV 时会当成公式执行。
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app import config

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="没装 node")


def _js(expr: str):
    url = (config.ROOT / "web" / "js" / "csv.js").as_uri()
    code = f"import {{ csvCell, seriesToCsv }} from {json.dumps(url)};\n" \
           f"process.stdout.write(JSON.stringify({expr}));"
    # node 吐的是 UTF-8。text=True 会按系统编码解（Windows 上是 cp1252），中文和 BOM 就全乱了
    r = subprocess.run([NODE, "--input-type=module", "-e", code],
                       capture_output=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


@pytest.mark.parametrize("raw, want", [
    ("=HYPERLINK(\"http://x\")", "\"'=HYPERLINK(\"\"http://x\"\")\""),   # 公式：前面加 '，再按 CSV 规矩加引号
    ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("-样品", "'-样品"),
    ("\tTAB", "'\tTAB"),
    ("ZG0013_S1", "ZG0013_S1"),                                        # 普通名字不动
    ("a,b", "\"a,b\""),
])
def test_text_cells_cannot_become_formulas(raw, want):
    assert _js(f"csvCell({json.dumps(raw)})") == want


def test_numbers_stay_numbers():
    """负数也以 - 开头，但它是数：原样写，不然一列数就全变成文字了。"""
    assert _js("[csvCell(-0.5), csvCell('-0.5'), csvCell(1e-3), csvCell(0), csvCell(NaN), csvCell(null)]") \
        == ["-0.5", "-0.5", "0.001", "0", "", ""]


def test_a_whole_export_with_a_hostile_sample_name():
    csv = _js("seriesToCsv({x_label: 't (s)', series: ["
              "{label: '=1+1', x: [0, 1], y: [-2, 3]},"
              "{label: 'ZG0014', x: [0, 1], y: [4, NaN]}]})")
    lines = csv.lstrip("﻿").splitlines()
    assert lines == ["t (s),'=1+1,ZG0014", "0,-2,4", "1,3,"]
