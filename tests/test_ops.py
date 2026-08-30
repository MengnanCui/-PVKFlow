"""算子集的一致性测试 —— A 档的承重墙。

每个算子有两份实现：Python（批处理用）和 JS（拖动时在浏览器里跑）。
两份必须给出**逐点相同**的数。

为什么这条测试比它看起来重要得多：两份实现一旦漂了，症状是
「你在界面上拖出来一个数、存进库里的是另一个数」，而且**没有任何报错**。
图看着正常、表看着正常，只是它们说的不是同一件事。
这类错误在小数据集上根本发现不了，等发现的时候已经用它写过报告了。

所以：加算子必须同时加 Python、JS 和这里的一条用例。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import numpy as np
import pytest

from app import config
from app.modules import ops

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="没装 node，跑不了 JS 那一半。装了 node 之后这条测试才真正起作用。")


def _run_js(op_name: str, lam, M, args) -> list:
    """在 node 里跑 web/js/ops.js 的同一个算子。"""
    ops_url = (config.ROOT / "web" / "js" / "ops.js").as_uri()
    # 走 stdin 而不是命令行参数：矩阵的 JSON 有几十 KB，参数长度是有上限的
    harness = textwrap.dedent(f"""
        import {{ runOp }} from {json.dumps(ops_url)};
        let raw = '';
        process.stdin.setEncoding('utf8');
        process.stdin.on('data', (c) => {{ raw += c; }});
        process.stdin.on('end', () => {{
          const inp = JSON.parse(raw);
          const frames = {{ lambda: inp.lam, values: inp.M, time: inp.t }};
          process.stdout.write(JSON.stringify(runOp(inp.op, frames, inp.args)));
        }});
    """)
    payload = json.dumps({
        "op": op_name,
        "lam": [float(x) for x in lam],
        "M": [[float(v) for v in row] for row in M],
        "t": list(range(M.shape[1])),
        "args": args,
    })
    r = subprocess.run([NODE, "--input-type=module", "-e", harness],
                       input=payload, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise AssertionError(f"node 跑失败：{r.stderr[-800:]}")
    return json.loads(r.stdout)


def _matrix(seed=0, n_lam=140, n_t=25):
    rng = np.random.default_rng(seed)
    lam = np.linspace(600.3, 1100.7, n_lam)        # 故意不是整数边界
    M = rng.normal(1.0, 0.3, (n_lam, n_t))
    return lam, M


CASES = [
    ("band_integral", {"band": [800, 950]}),
    ("band_integral", {"band": [612.5, 1099.1]}),      # 贴着两端
    ("band_integral", {"band": [900.0, 900.4]}),       # 窄到只剩一两个采样点
    ("wavelength_slope", {"center": 950, "half": 10}),
    ("wavelength_slope", {"center": 700.25, "half": 3.5}),
    ("wavelength_slope", {"center": 1050, "half": 40}),
]


@pytest.mark.parametrize("op_name,args", CASES)
def test_python_and_js_agree_pointwise(op_name, args):
    lam, M = _matrix(seed=hash((op_name, str(args))) % 1000)
    py = ops.run(op_name, M, lam, args)
    js = _run_js(op_name, lam, M, args)

    assert len(js) == len(py), f"长度不一样：JS {len(js)} vs Python {len(py)}"
    for i, (a, b) in enumerate(zip(py, js)):
        if np.isnan(a):
            assert b is None, f"第 {i} 点：Python 是 NaN，JS 给了 {b}"
            continue
        assert b is not None, f"第 {i} 点：Python 给了 {a}，JS 是 null"
        assert abs(a - b) <= 1e-9 * max(1.0, abs(a)), (
            f"第 {i} 点对不上：Python {a!r} vs JS {b!r}")


def test_every_python_op_has_a_js_twin():
    """两边的算子名必须完全一致。

    只加一边是最危险的情况：Python 有、JS 没有 → 拖动时前端抛错；
    JS 有、Python 没有 → 拖出来的曲线批处理时复现不了。
    """
    js_src = (config.ROOT / "web" / "js" / "ops.js").read_text(encoding="utf-8")
    block = js_src.split("export const OPS = {", 1)[1].split("};", 1)[0]
    js_names = {line.split(":")[0].strip() for line in block.splitlines()
                if ":" in line and not line.strip().startswith("//")}
    assert js_names == set(ops.OPS), (
        f"算子集不同步：只有 Python 有 {set(ops.OPS) - js_names}，"
        f"只有 JS 有 {js_names - set(ops.OPS)}")


def test_an_unknown_op_names_the_ones_that_exist():
    """报错要能直接改 —— 模块作者（和他的模型）就靠这句话。"""
    with pytest.raises(KeyError) as e:
        ops.get("band_integrl")            # 打错一个字母
    msg = str(e.value)
    assert "band_integral" in msg and "wavelength_slope" in msg


def test_a_missing_arg_says_which_one():
    lam, M = _matrix()
    with pytest.raises(ValueError) as e:
        ops.run("wavelength_slope", M, lam, {"center": 950})
    assert "half" in str(e.value)
