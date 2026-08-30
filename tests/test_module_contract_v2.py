"""契约扩展（多序列 / 面板类型 / stats / notice）。

这一组的第一条是最重要的：**扩展不能逼着已有模块改一个字**。
逼着改的话，同事上周写的模块这周就装不上了 —— 那这套东西就没人敢用。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.modules import validate
from app.modules.base import (SERIES_COLORS, Control, Curve, Module, ModuleContext,
                              ModuleSpec, Notice, Op, Panel, PanelData, Series, Stat)
from app.modules.registry import ModuleRegistry, TEMPLATE_DIR

BASE = dict(id="t.x", name="试", version="1.0.0")
BAND = Control("b", "波段", "band", default=[800, 950])


def _mod(spec, compute=None):
    m = Module()
    m.spec = spec
    if compute:
        m.compute = compute.__get__(m, Module)
    return m


def _ctx(params=None, n_t=20):
    lam = np.linspace(600, 1100, 120)
    t = np.linspace(0, 10, n_t)
    return ModuleContext(lam, np.random.rand(120, n_t), t, params or {"b": [800, 950]})


# ---------------------------------------------------------------- 向后兼容
def test_the_simple_way_of_writing_a_panel_is_unchanged():
    """`PanelData(x=..., y=...)` 还是那么写。九成的模块只需要这个。"""
    d = PanelData(x=[0, 1, 2], y=[1.0, 2.0, 3.0])
    j = d.as_dict()
    assert j["x"] == [0, 1, 2] and j["y"] == [1.0, 2.0, 3.0]
    # 前端只吃 series，所以单条也归一成 series —— 但契约这边不用写它
    assert len(j["series"]) == 1
    assert j["series"][0]["y"] == [1.0, 2.0, 3.0]


def test_extending_the_contract_did_not_break_the_shipped_modules(workspace):
    """平台自带的模块和模板，扩展之后一个字都不用改。

    要是逼着它们改，说明这次扩展设计坏了 —— 同事上周写的模块这周就装不上。
    """
    import shutil

    reg = ModuleRegistry()
    reg.load_all()
    assert reg.all(), "一个模块都没装上"
    for m in reg.all():
        r = validate.validate(m)
        assert r.ok, f"{m.spec.id} 装不上了：{r.errors}"

    # 模板也一样
    dest = config_tmp = None
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        dest = f"{tmp}/t"
        shutil.copytree(TEMPLATE_DIR, dest)
        probe = ModuleRegistry()
        probe._load_one(__import__("pathlib").Path(dest) / "module.py", "user",
                        __import__("pathlib").Path(dest))
        assert validate.validate(probe.all()[0]).ok


def test_nan_becomes_null_not_a_crash():
    """json 里没有 NaN 这个东西。不转的话前端拿到的是坏 json。"""
    j = PanelData(x=[0, 1], y=[1.0, float("nan")]).as_dict()
    assert j["y"] == [1.0, None]
    j2 = PanelData(x=[0], y=[float("inf")]).as_dict()
    assert j2["y"] == [None]


# ---------------------------------------------------------------- 多序列
def test_a_panel_can_draw_several_series():
    """膜厚那张 OT 图是三条叠出来的：灰线打底 + 琥珀散点 + 绿散点。"""
    d = PanelData(series=[
        Series([0, 1], [1, 2], "全部帧", "line", "muted"),
        Series([1], [2], "可信", "scatter", "ok"),
    ])
    j = d.as_dict()
    assert [s["label"] for s in j["series"]] == ["全部帧", "可信"]
    assert [s["style"] for s in j["series"]] == ["line", "scatter"]
    assert j["series"][0]["color"] == SERIES_COLORS["muted"]


def test_a_hex_colour_is_refused_and_the_allowed_names_are_listed():
    """颜色只收语义名。收 hex 的话调色板守不住，暗色主题下还会瞎掉 ——
    而「风格不会漂」正是整套模块化的卖点。"""
    def compute(self, ctx):
        return {"p": PanelData(series=[Series(list(ctx.t), [1] * len(ctx.t),
                                              color="#ff00ff")])}

    m = _mod(ModuleSpec(**BASE, controls=[BAND],
                        panels=[Panel("p", "图", uses=["b"])]), compute)
    r = validate.validate(m)
    assert not r.ok
    msg = "\n".join(r.errors)
    assert "#ff00ff" in msg
    assert "ok" in msg and "muted" in msg          # 把能用的列出来
    assert "十六进制" in msg


def test_an_unknown_style_says_which_ones_exist():
    def compute(self, ctx):
        return {"p": PanelData(series=[Series(list(ctx.t), [1] * len(ctx.t),
                                              style="dashed")])}

    m = _mod(ModuleSpec(**BASE, controls=[BAND],
                        panels=[Panel("p", "图", uses=["b"])]), compute)
    r = validate.validate(m)
    assert not r.ok
    assert "line+scatter" in "\n".join(r.errors)


def test_mismatched_series_lengths_name_the_series():
    def compute(self, ctx):
        return {"p": PanelData(series=[Series([0, 1, 2], [1, 2], "我的曲线")])}

    m = _mod(ModuleSpec(**BASE, controls=[BAND],
                        panels=[Panel("p", "图", uses=["b"])]), compute)
    r = validate.validate(m)
    assert not r.ok
    assert "我的曲线" in "\n".join(r.errors)


# ---------------------------------------------------------------- 面板类型
def test_a_heatmap_panel_without_an_image_url_says_what_to_do():
    def compute(self, ctx):
        return {"p": PanelData()}

    m = _mod(ModuleSpec(**BASE, controls=[BAND],
                        panels=[Panel("p", "条纹", uses=["b"], kind="heatmap")]), compute)
    r = validate.validate(m)
    assert not r.ok
    msg = "\n".join(r.errors)
    assert "image_url" in msg and "heatmap" in msg


def test_a_text_panel_without_text_says_what_to_do():
    def compute(self, ctx):
        return {"p": PanelData()}

    m = _mod(ModuleSpec(**BASE, controls=[BAND],
                        panels=[Panel("p", "报告", uses=["b"], kind="text")]), compute)
    r = validate.validate(m)
    assert not r.ok
    assert "text" in "\n".join(r.errors)


def test_an_unknown_panel_kind_lists_the_three():
    r = validate.check_spec(ModuleSpec(
        **BASE, controls=[BAND], panels=[Panel("p", "图", uses=["b"], kind="pie")]))
    assert not r.ok
    msg = "\n".join(r.errors)
    assert "pie" in msg and "heatmap" in msg and "text" in msg


def test_a_non_xy_panel_cannot_use_an_operator():
    """算子产出的是曲线。挂在热力图面板上是个概念错误，要当场说清。"""
    r = validate.check_spec(ModuleSpec(
        **BASE, controls=[BAND],
        panels=[Panel("p", "条纹", uses=["b"], kind="heatmap",
                      live=Op.band_integral(band="b"))]))
    assert not r.ok
    assert "live=" in "\n".join(r.errors)


def test_a_full_strength_panel_passes():
    """用满新能力的一格：三条序列 + 带 ⓘ 的数字 + 提示块 + ⓘ 附加段。
    这正是膜厚那张 OT 图的形状。"""
    def compute(self, ctx):
        n = len(ctx.t)
        return {"p": PanelData(
            series=[Series(list(ctx.t), [1.0] * n, "全部帧", "line", "muted"),
                    Series(list(ctx.t), [2.0] * n, "可信", "scatter", "ok")],
            stats=[Stat("可测下限", 351, "nm", info="ot_floor"),
                   Stat("量化格距", 29, "nm", info="ot_quantum")],
            notice=Notice("warn", "这一格是对照", "不是测量结果"),
            info_extra={"title": "判级", "items": [{"key": "OK", "label": "可信"}]},
            y_label="光学厚度 (nm)")}

    m = _mod(ModuleSpec(**BASE, controls=[BAND],
                        panels=[Panel("p", "OT vs 时间", uses=["b"], info="ot")]), compute)
    r = validate.validate(m)
    assert r.ok, r.errors


def test_a_stat_pointing_at_a_missing_term_is_caught():
    def compute(self, ctx):
        n = len(ctx.t)
        return {"p": PanelData(x=list(ctx.t), y=[1.0] * n,
                               stats=[Stat("下限", 1, "nm", info="ot_flooor")])}

    m = _mod(ModuleSpec(**BASE, controls=[BAND],
                        panels=[Panel("p", "图", uses=["b"])]), compute)
    r = validate.validate(m)
    assert not r.ok
    assert "ot_flooor" in "\n".join(r.errors)


# ---------------------------------------------------------------- 服务端出图的参数
def test_a_colour_map_the_platform_does_not_have_is_caught_before_install():
    """`cmap="turbo"` 要在装之前就报出来，不能等到页面上出一个红框。

    浏览器那边只会显示「无法渲染这张图（HTTP 422）」—— 图没了，
    可为什么没了、能写哪几个，一个字都没有。验证器会真跑一遍 compute()，
    所以在 image_url() 里抛，就变成装模块之前看得见的一条明确报错。
    """
    ctx = ModuleContext(np.linspace(600, 1100, 20), np.zeros((20, 5)),
                        np.linspace(0, 1, 5), {}, artifact_id="art_x")
    with pytest.raises(ValueError) as e:
        ctx.image_url("heatmap.png", cmap="turbo")
    msg = str(e.value)
    assert "turbo" in msg and "rainbow" in msg          # 说清写错了什么、能写什么
    assert "你是不是想写" in msg                          # 而且猜一个


def test_the_allowed_values_are_read_from_render_not_copied():
    """白名单只有一份：`app/parsers/render.py`。

    抄第二份的话，加一个色标就得记着改两处 —— 漏改的那处只会在运行时 422，
    而 422 是最难查的那种错：模块自己看着完全正常。
    """
    from app.parsers import render

    ctx = ModuleContext(np.linspace(600, 1100, 20), np.zeros((20, 5)),
                        np.linspace(0, 1, 5), {}, artifact_id="art_x")
    for name in render.COLORMAPS:
        assert ctx.image_url("heatmap.png", cmap=name)      # 一个都不能被拒
    for axis in render.AXES:
        assert ctx.image_url("heatmap.png", axis=axis)
    for norm in render.NORMS:
        assert ctx.image_url("heatmap.png", norm=norm)


def test_an_unknown_render_endpoint_lists_the_real_ones():
    ctx = ModuleContext(np.linspace(600, 1100, 20), np.zeros((20, 5)),
                        np.linspace(0, 1, 5), {}, artifact_id="art_x")
    with pytest.raises(ValueError) as e:
        ctx.image_url("fringe.jpg")
    assert "heatmap.png" in str(e.value)


def test_a_module_using_every_new_capability_installs_and_runs(workspace, tmp_path):
    """同事只用文档里写出来的东西，能不能做到膜厚那个复杂度？

    这一条把新契约的每一样都用一遍 —— 多序列 + 语义色 + heatmap + text +
    notice + 带 ⓘ 的 stats + info_extra + batch_extra + batch_all_frames +
    ctx.needs() —— 装进 workspace，验证器过，跑出来的东西形状对。

    验收线是「同事能做的事和平台自己同级」。这条不过，那句话就是空的。
    """
    from app.modules.registry import ModuleRegistry

    src = (tmp_path / "demo_full")
    src.mkdir()
    (src / "module.py").write_text(_FULL_STRENGTH, encoding="utf-8")

    reg = ModuleRegistry()
    reg._load_one(src / "module.py", "user", src)
    assert reg.all(), "装不上"
    mod = reg.all()[0]

    r = validate.validate(mod)
    assert r.ok, r.errors

    ctx = _ctx({"band": [800, 950], "thresh": 0.5}, n_t=25)
    ctx.artifact_id = "art_demo"
    out = mod.compute(ctx)

    assert set(out) == {"map", "peak", "slow", "note"}
    assert out["map"].image_url.startswith("/api/spectra/art_demo/heatmap.png")
    assert out["note"].text and "\n" in out["note"].text
    assert len(out["peak"].series) >= 1
    assert out["peak"].series[0].color == "muted"
    assert [s.label for s in out["peak"].stats][:2] == ["共", "过阈值"]
    assert out["slow"].notice and out["slow"].notice.kind == "info"
    assert len(out["peak"].batch_extra["ok"]) == 25

    # ctx.needs()：说了只要 peak，那格贵的 slow 就该被跳过
    ctx2 = _ctx({"band": [800, 950], "thresh": 0.5}, n_t=25)
    ctx2.artifact_id = "art_demo"
    ctx2._needed = {"peak"}
    assert "slow" not in mod.compute(ctx2)


_FULL_STRENGTH = '''
import numpy as np
from app.modules.base import (Control, Curve, Module, ModuleSpec, Notice, Panel,
                              PanelData, Series, Stat)


class DemoFull(Module):
    spec = ModuleSpec(
        id="demo.full", name="示范 · 用满契约", version="1.0.0",
        order=900, columns=2,
        controls=[
            Control("band", "波段", "band", default=[800, 950], unit="nm",
                    range_from="lambda"),
            Control("thresh", "阈值", "number", default=1.0, unit="a.u."),
        ],
        panels=[
            Panel("map", "原始矩阵", kind="heatmap", info="k_axis", y_label="波长 (nm)"),
            Panel("peak", "峰值 vs 时间", uses=["band", "thresh"], info="integral",
                  y_label="峰值强度 (a.u.)"),
            Panel("slow", "全波段峰值（对照）", info="integral",
                  y_label="峰值强度 (a.u.)"),
            Panel("note", "说明", kind="text", span=1),
        ],
        batch_curves=[Curve("demo_peak", from_panel="peak"),
                      Curve("demo_ok", from_panel="peak", key="ok")],
        batch_all_frames=True,
    )

    def compute(self, ctx):
        out = {}
        lo, hi = ctx.param("band")
        thr = float(ctx.param("thresh"))
        out["map"] = PanelData(
            image_url=ctx.image_url("heatmap.png", norm="frame", cmap="rainbow"),
            x_range=[float(ctx.t[0]), float(ctx.t[-1])],
            y_range=[float(ctx.lam[0]), float(ctx.lam[-1])],
            v_range=[0, 1], v_label="每帧归一化", cmap="rainbow",
            caption="整幅矩阵")
        if ctx.needs("slow"):
            out["slow"] = PanelData(
                x=list(ctx.t), y=[float(v) for v in ctx.M.max(axis=0)],
                notice=Notice("info", "这一格不跟着控件走", "放这儿当参照"))
        mask = (ctx.lam >= lo) & (ctx.lam <= hi)
        y = ctx.M[mask].max(axis=0) if mask.any() else np.full(len(ctx.t), np.nan)
        xs = [float(v) for v in ctx.t]
        ys = [None if not np.isfinite(v) else float(v) for v in y]
        ok = [(v is not None and v >= thr) for v in ys]
        series = [Series(xs, ys, "全部帧", "line", "muted")]
        ok_x = [x for x, k in zip(xs, ok) if k]
        if ok_x:
            series.append(Series(ok_x, [v for v, k in zip(ys, ok) if k],
                                 "过阈值", "scatter", "ok"))
        n_ok = sum(ok)
        out["peak"] = PanelData(
            series=series,
            stats=[Stat("共", len(xs), "帧"),
                   Stat("过阈值", n_ok, "帧", tone="ok" if n_ok else "warn"),
                   Stat("可测下限", 351, "nm", info="ot_floor")],
            info_extra={"title": "分档", "items": [{"key": "ok", "label": "过阈值",
                                                   "count": n_ok}]},
            batch_extra={"ok": [1.0 if k else 0.0 for k in ok]})
        out["note"] = PanelData(text="波段 %g – %g nm\\n帧数 %d" % (lo, hi, len(xs)))
        return out


MODULE = DemoFull()
'''
