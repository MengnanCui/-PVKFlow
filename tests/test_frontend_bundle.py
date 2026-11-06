"""前端没有构建链，所以「语法写错了」「写出了一个 import 环」平时没有任何东西拦。

借离线烤制工具的打包器验一遍：它会检查三条前提（只有两种 import、没有 export default、
依赖无环），打出来的是一个普通脚本，再交给 node 做语法检查。
CI 的每个任务都会跑到这里；本机没装 node 就跳过。
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess

import pytest

from app import config


def _bake():
    spec = importlib.util.spec_from_file_location(
        "bake_offline", config.ROOT / "tools" / "bake_offline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_frontend_module_bundles_without_cycles_or_default_exports():
    js, n_modules = _bake().bundle_js()
    assert n_modules >= 20
    assert "__req('app.js')" in js


@pytest.mark.skipif(not shutil.which("node"), reason="没装 node")
def test_the_bundled_frontend_is_valid_javascript(tmp_path):
    js, _ = _bake().bundle_js()
    out = tmp_path / "bundle.js"
    out.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(out)], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, r.stderr
