"""docs/REFERENCE.md 是从代码生成的（tools/gen_reference.py）。改了接口 / 页面 / 智能体工具 / 表 / 环境变量 / QFLS 阈值
却忘了重新生成，这条测试就红 —— 文档和代码对不上，是接手的人（或别的模型）最容易被坑的地方。"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_reference_doc_is_up_to_date(workspace):
    spec = importlib.util.spec_from_file_location("gen_reference", ROOT / "tools" / "gen_reference.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    current = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert current == gen.build(), "docs/REFERENCE.md 过期了：跑 .venv/bin/python tools/gen_reference.py"
