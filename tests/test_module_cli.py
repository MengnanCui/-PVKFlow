"""本地 CLI 验证。

**最要紧的一条是「和界面给出逐字相同的报错」。** 两份文案一旦漂了，
同事按命令行改到通过、导入还是不过 —— 那比没有这个命令更糟，
因为他会开始怀疑这两个东西哪个能信。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app import config
from app.modules import check as cli
from app.modules import validate
from app.modules.registry import ModuleRegistry, TEMPLATE_DIR

BAD = '''
    from app.modules.base import Module, ModuleSpec, Control, Panel, Op

    class M(Module):
        spec = ModuleSpec(
            id="cli.bad", name="坏模块", version="1.0.0",
            controls=[Control("band", "波段", "band", default=[800, 950])],
            panels=[Panel("p", "图", uses=["bnd"],          # ← 打错一个字母
                          live=Op.band_integral(band="band"))],
        )

    MODULE = M()
'''


def _write(d: Path, body: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "module.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return d


def test_the_cli_and_the_import_path_say_exactly_the_same_thing(workspace, tmp_path):
    """同一个模块，命令行和界面导入必须给出**逐字相同**的报错。"""
    folder = _write(tmp_path / "bad", BAD)

    # 命令行那条路
    ok_cli, lines = cli.check(folder)
    cli_errors = [l[2:] for l in lines if l.startswith("✗ ")]

    # 界面导入那条路（同一个函数，导入接口内部就调它）
    from app.api.modules import _validate_folder
    report = _validate_folder(folder)

    assert not ok_cli and not report["ok"]
    assert cli_errors == report["errors"], "命令行和界面的报错不一样了"


def test_a_clean_module_exits_zero(workspace, tmp_path):
    import shutil
    dest = tmp_path / "t"
    shutil.copytree(TEMPLATE_DIR, dest)
    assert cli.main([str(dest)]) == 0


def test_a_broken_module_exits_one(workspace, tmp_path):
    folder = _write(tmp_path / "bad", BAD)
    assert cli.main([str(folder)]) == 1


def test_pointing_at_a_folder_without_module_py_says_so(workspace, tmp_path):
    (tmp_path / "empty").mkdir()
    ok, lines = cli.check(tmp_path / "empty")
    assert not ok
    text = "\n".join(lines)
    assert "module.py" in text and "_template" in text


def test_it_accepts_the_file_as_well_as_the_folder(workspace, tmp_path):
    """`check 我的模块` 和 `check 我的模块/module.py` 都该认 ——
    命令行补全经常直接补到文件上。"""
    folder = _write(tmp_path / "bad", BAD)
    a = cli.check(folder)
    b = cli.check(folder / "module.py")
    assert a[0] == b[0]
    assert [l for l in a[1] if l.startswith("✗")] == [l for l in b[1] if l.startswith("✗")]


def test_the_failure_output_tells_you_to_paste_it_to_your_model(workspace, tmp_path):
    """这一句是整个「弱模型也能收敛」的闭环里最后一环 ——
    人得知道这段报错是可以直接喂回去的。"""
    folder = _write(tmp_path / "bad", BAD)
    _, lines = cli.check(folder)
    assert any("贴给你的模型" in l for l in lines)


def test_the_shipped_modules_all_pass_the_cli(workspace):
    """平台自带的模块必须过命令行验证 —— 它们是同事照着改的示范。"""
    reg = ModuleRegistry()
    reg.load_all()
    assert reg.all()
    for m in reg.all():
        folder = reg.dir_of(m.spec.id)
        ok, lines = cli.check(folder)
        assert ok, f"{m.spec.id} 没过：{lines}"
