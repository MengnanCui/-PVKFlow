"""功能模块：发现、隔离、验证。

这一组测试的重点和别处不太一样：**大部分断言是在检查报错信息说清楚了没有**。
因为这些报错的读者是同事的模型 —— 说不清楚，「报错 → 改 → 再试」这个循环
就收敛不了，模块化这件事也就落不了地。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import numpy as np
import pytest

from app import config
from app.modules import ops, validate
from app.modules.base import (Control, Curve, Module, ModuleContext, ModuleSpec,
                              Op, Panel, PanelData)
from app.modules.registry import ModuleRegistry, registry

BASE = dict(id="pl.peak", name="荧光", version="1.0.0")
BAND = Control("integ", "波段", "band", default=[800, 950], unit="nm")
NUM = Control("c", "波长", "number", default=950)


def _mod(spec: ModuleSpec) -> Module:
    m = Module()
    m.spec = spec
    return m


def _good_spec(**over) -> ModuleSpec:
    d = dict(BASE, controls=[BAND],
             panels=[Panel("i", "积分", uses=["integ"], info="integral",
                           live=Op.band_integral(band="integ"))],
             batch_curves=[Curve("integral", from_panel="i")])
    d.update(over)
    return ModuleSpec(**d)


# ---------------------------------------------------------------- 契约本身
def test_a_pure_declaration_module_needs_no_algorithm_code():
    """A 档模块一行算法都不用写 —— 基类拿声明里的算子跑出来。

    这是整个设计的支点：同事不写算法，就不会写错算法；
    不写渲染，风格就不会漂。
    """
    m = _mod(_good_spec())
    lam = np.linspace(600, 1100, 120)
    t = np.linspace(0, 10, 20)
    out = m.compute(ModuleContext(lam, np.random.rand(120, 20), t, m.spec.defaults()))

    assert set(out) == {"i"}
    assert len(out["i"].y) == len(t)
    # Y 轴标签也是从算子来的 —— 同事不用重复写单位
    assert out["i"].y_label == ops.get("band_integral").y_label


def test_a_typo_in_an_op_name_fails_at_definition_time():
    """打错算子名要在**写模块的那一刻**就报错，而不是装上之后才发现。"""
    with pytest.raises(KeyError) as e:
        Op.band_integrl(band="x")
    assert "band_integral" in str(e.value)      # 把现有的列出来


# ---------------------------------------------------------------- 验证器
# 每条都断言「报错里出现了那个字段名」—— 说不清楚等于没有报错
def test_a_wrong_control_key_names_the_real_ones_and_guesses():
    r = validate.check_spec(ModuleSpec(
        **BASE, controls=[BAND, NUM],
        panels=[Panel("i", "积分", uses=["integral"],
                      live=Op.band_integral(band="integ"))]))
    msg = "\n".join(r.errors)
    assert not r.ok
    assert '"integral"' in msg and "integ" in msg
    assert "你是不是想写" in msg              # 打错一个字母是最常见的错


def test_a_control_of_the_wrong_type_says_which_types_would_work():
    r = validate.check_spec(ModuleSpec(
        **BASE, controls=[BAND, NUM],
        panels=[Panel("i", "积分", uses=["c"], live=Op.band_integral(band="c"))]))
    msg = "\n".join(r.errors)
    assert not r.ok
    assert "band" in msg and "number" in msg
    assert "integ" in msg                      # 告诉他哪个控件是对的


def test_an_unknown_glossary_term_is_rejected():
    """ⓘ 指向不存在的术语，点开就是空的 —— 那比没有 ⓘ 更糟。"""
    r = validate.check_spec(_good_spec(
        panels=[Panel("i", "积分", uses=["integ"], info="integrl",
                      live=Op.band_integral(band="integ"))]))
    assert not r.ok
    assert "integrl" in "\n".join(r.errors)


def test_a_batch_curve_pointing_at_no_panel_lists_the_panels():
    r = validate.check_spec(_good_spec(
        batch_curves=[Curve("integral", from_panel="nope")]))
    msg = "\n".join(r.errors)
    assert not r.ok and "nope" in msg
    assert "现有的面板：i" in msg


@pytest.mark.parametrize("bad,needle", [
    (dict(id="PLPeak"), "id"),
    (dict(name=""), "name"),
    (dict(version="1.0"), "version"),
])
def test_malformed_identity_fields_say_what_the_format_is(bad, needle):
    r = validate.check_spec(_good_spec(**bad))
    assert not r.ok
    assert needle in "\n".join(r.errors)


def test_a_duplicate_id_is_refused_with_a_way_out():
    r = validate.check_spec(_good_spec(), known_ids={"pl.peak"})
    assert not r.ok
    assert "卸载" in "\n".join(r.errors)       # 给出下一步，不只是说「重名」


def test_defaults_outside_the_data_range_are_caught_by_the_trial_run():
    """声明完全合法，但默认波段落在数据范围外 —— 装上去就是一条空曲线。
    这种只有真跑一遍才发现。"""
    m = _mod(_good_spec(
        controls=[Control("integ", "波段", "band", default=[2000, 2200])],
        panels=[Panel("i", "积分", uses=["integ"],
                      live=Op.band_integral(band="integ"))]))
    r = validate.validate(m)
    assert not r.ok
    assert "全是空值" in "\n".join(r.errors)
    assert "600" in "\n".join(r.errors)        # 告诉他实际范围是多少


def test_a_crashing_compute_reports_the_traceback_not_just_failed():
    class Boom(Module):
        # B 档面板（没有 live=），batch_curves 也跟着指到它
        spec = _good_spec(panels=[Panel("b", "炸", uses=["integ"])],
                          batch_curves=[Curve("boom", from_panel="b")])

        def compute(self, ctx):
            raise ValueError("我崩了")

    r = validate.validate(Boom())
    assert not r.ok
    msg = "\n".join(r.errors)
    assert "我崩了" in msg and "ValueError" in msg


def test_forgetting_super_in_compute_is_explained():
    """重写 compute() 但忘了调 super()，A 档面板就没了。
    这个错很隐蔽，报错要直接点出解法。"""
    class Forgot(Module):
        spec = _good_spec()

        def compute(self, ctx):
            return {}          # 忘了 super().compute(ctx)

    r = validate.validate(Forgot())
    assert not r.ok
    assert "super().compute" in "\n".join(r.errors)


def test_a_clean_module_passes():
    r = validate.validate(_mod(_good_spec()))
    assert r.ok, r.errors
    assert not r.warnings
    assert "试跑" in r.checked


# ---------------------------------------------------------------- 发现与隔离
def _write_module(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "module.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return d


GOOD = '''
    from app.modules.base import Module, ModuleSpec, Control, Panel, Op

    class M(Module):
        spec = ModuleSpec(
            id="test.good", name="好模块", version="1.0.0",
            controls=[Control("b", "波段", "band", default=[800, 950])],
            panels=[Panel("p", "积分", uses=["b"], live=Op.band_integral(band="b"))],
        )

    MODULE = M()
'''


def test_dropping_a_folder_in_is_all_it_takes(workspace):
    """放个文件夹进去、重载一下就生效。不重启、不改平台代码、不碰 Git。"""
    _write_module(config.MODULES_DIR, "good", GOOD)
    reg = ModuleRegistry()
    reg.load_all()

    ids = [m.spec.id for m in reg.all()]
    assert "test.good" in ids
    assert reg.get("test.good").spec.origin == "user"
    # 平台自带的那个也在 —— 它是活示范，同事照着它改
    assert "special.slope_integral" in ids
    assert not reg.errors


def test_one_broken_module_does_not_take_the_platform_down(workspace):
    """同事放进来一个写错的模块，平台照常起来，那一条显示为失败 + 原因。

    这条是「敢让别人往里放东西」的前提 —— 一个坏模块能弄死平台的话，
    没人敢装第二个。
    """
    _write_module(config.MODULES_DIR, "good", GOOD)
    _write_module(config.MODULES_DIR, "broken", "this is not python (")
    _write_module(config.MODULES_DIR, "empty", "x = 1")      # 里面没有模块

    reg = ModuleRegistry()
    reg.load_all()

    assert "test.good" in [m.spec.id for m in reg.all()]     # 好的照常装上
    sources = " ".join(e["source"] + e["error"] for e in reg.errors)
    assert "broken" in sources and "empty" in sources
    assert len(reg.errors) == 2
    # 「没找到模块」要说清楚该怎么写
    empty_err = next(e for e in reg.errors if "empty" in e["source"])
    assert "MODULE" in empty_err["error"]


def test_a_folder_without_module_py_says_what_is_missing(workspace):
    (config.MODULES_DIR / "nofile").mkdir(parents=True, exist_ok=True)
    reg = ModuleRegistry()
    reg.load_all()
    assert len(reg.errors) == 1
    assert "module.py" in reg.errors[0]["error"]
    assert "_template" in reg.errors[0]["detail"]    # 指个路


def test_reload_picks_up_a_module_added_after_startup(workspace):
    reg = ModuleRegistry()
    reg.load_all()
    assert "test.good" not in [m.spec.id for m in reg.all()]

    _write_module(config.MODULES_DIR, "good", GOOD)
    reg.load_all()                                   # 这就是界面上那个「重载」
    assert "test.good" in [m.spec.id for m in reg.all()]


# ---------------------------------------------------------------- 活示范
def test_the_builtin_module_passes_its_own_validator(workspace):
    """平台自带的「特殊处理」必须过自己的验证器。

    它是给同事看的活示范 —— 示范本身不合规的话，这套契约就是一句空话。
    """
    reg = ModuleRegistry()
    reg.load_all()
    mod = reg.get("special.slope_integral")
    r = validate.validate(mod)
    assert r.ok, r.errors
    assert not r.warnings, r.warnings


def test_the_builtin_module_is_pure_declaration(workspace):
    """它没有自己的 compute()，两个面板全靠算子拼 —— 一行算法都没写。

    这条是在钉住「A 档真的够用」这个说法：平台自己的一个功能
    完整地走完了那条路，同事就不用怀疑它是不是只对玩具管用。
    """
    reg = ModuleRegistry()
    reg.load_all()
    mod = reg.get("special.slope_integral")
    assert type(mod).compute is Module.compute, "它重写了 compute()，那就不是纯声明了"
    assert all(p.live for p in mod.spec.panels), "有面板不是 A 档"


def test_the_template_installs_cleanly(workspace, tmp_path):
    """模板必须自己就能装上。

    一个装不上的模板是最坑人的东西 —— 同事复制它、改一改、装不上，
    然后花一小时怀疑是自己改错了。
    """
    import shutil

    from app.modules.registry import TEMPLATE_DIR

    dest = tmp_path / "copied"
    shutil.copytree(TEMPLATE_DIR, dest)

    reg = ModuleRegistry()
    reg._load_one(dest / "module.py", "user", dest)
    assert reg.all(), "模板里没找到模块"

    r = validate.validate(reg.all()[0])
    assert r.ok, r.errors
    assert not r.warnings, r.warnings


def test_the_template_is_seeded_into_the_workspace(workspace):
    """模板放在仓库里（workspace/ 是 gitignore 的），启动时播一份到工作区。

    文档里写「复制 workspace/modules/_template/」，那儿就得真的有东西。
    """
    from app.modules.registry import seed_template

    seed_template()
    assert (config.MODULES_DIR / "_template" / "module.py").is_file()

    # 下划线开头的目录不参与扫描 —— 模板本身不该出现在界面上
    reg = ModuleRegistry()
    reg.load_all()
    assert "my.module" not in [m.spec.id for m in reg.all()]
    assert not reg.errors


def test_every_info_in_a_builtin_module_exists_in_the_glossary(workspace):
    """ⓘ 指向的术语必须真的存在 —— 平台自己的模块也不例外。"""
    reg = ModuleRegistry()
    reg.load_all()
    terms = validate._glossary_terms()
    assert terms, "术语表读不出来"
    for m in reg.all():
        for p in m.spec.panels:
            if p.info:
                assert p.info in terms, f"{m.spec.id} 的 {p.id} 指向了不存在的术语 {p.info}"
