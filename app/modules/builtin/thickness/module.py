"""膜厚处理 —— 干涉条纹 → 光学厚度。

**这个模块是契约的压力测试。** 它是平台最难的一块：四宫格、服务端渲染的
条纹位图、三条序列叠出来的 OT 曲线、跟着数据走的 ⓘ、整幅宽的规范报告。
第一部分的契约表达不了它 —— 表达得了，才说明同事能做的事和平台自己同级。

算法全部来自 `app/analysis/fringe_ot.py`，那是 fringe-optical-thickness
冻结规范的可执行副本，**这里一个字都不改**：`DEFAULTS` 不动，
平台的窗口和门槛作为**显式 override** 传进去，报告块 A 回显的是实际用的值。

为什么是 B 档（松手才算）：一次 FFT 要跑 211 帧，拖着算既没必要也做不到。
平台在这件事上一直是这么做的 —— 便宜的操作能拖，贵的松手才算。
"""
import numpy as np

from app.analysis import fringe_ot
from app.modules.base import (Control, Curve, Module, ModuleSpec, Notice, Panel,
                              PanelData, Series, Stat)

# 平台默认窗口：775 避开约 775 nm 的吸收边，1120 是光谱仪上限。
# 这是**平台传的 override**，规范里的 DEFAULTS 仍是 780–1050。
BAND = list(fringe_ot.PLATFORM_WINDOW_NM)


class Thickness(Module):
    spec = ModuleSpec(
        id="thickness.fringe_ot",
        name="膜厚处理",
        version="1.0.0",
        description="干涉条纹的 FFT 分析：光学厚度 OT = n·d·cosθ 随时间的变化。",
        author="HTE Studio",
        order=10,
        columns=2,

        controls=[
            Control("band", "波段", "band", default=BAND, unit="nm",
                    range_from="lambda",
                    help="做 FFT 的波长窗口。窗口越宽，能测的膜越薄"),
        ],

        panels=[
            # ── 上排：全波段（对照用）
            Panel("fringe_full", "全波段条纹", kind="heatmap", info="k_axis",
                  y_label="k = 1/λ (nm⁻¹)"),
            Panel("ot_full", "全波段　光学厚度 vs 时间", info="ot",
                  y_label="光学厚度 OT = n·d·cosθ (nm)"),
            # ── 下排：指定波段（真正的结果）
            Panel("fringe_band", "指定波段条纹", kind="heatmap", uses=["band"],
                  info="band", y_label="k = 1/λ (nm⁻¹)"),
            Panel("ot_band", "指定波段　光学厚度 vs 时间", info="ot",
                  y_label="光学厚度 OT = n·d·cosθ (nm)"),
            # ── 整幅宽：规范 §5 的块 A–D
            Panel("report", "完整报告", kind="text", span=1, uses=["band"],
                  caption="fringe-optical-thickness 规范 §5 要求块 A–D 一个都不能少"),
        ],

        # 批处理要两列：膜厚本身，和每帧可不可信。
        #
        # 判级不画在曲线上（画了满屏都是标记，反而看不见哪段能用），
        # 但「1 s 内的平均膜厚」光有均值没法判断可不可靠 —— 所以它得跟着进长表。
        # 走 `batch_extra`，见 _ot_panel 结尾。
        batch_curves=[
            Curve("ot", from_panel="ot_band"),
            Curve("ot_ok", from_panel="ot_band", key="ot_ok"),
        ],
        batch_metrics=["ot_floor", "fringe_bin", "ot_ok_frames", "ot_frames",
                       "ot_first_ok", "ot_last_ok"],
        # 膜厚必须逐帧算，不接受抽稀 —— 见 ModuleSpec.batch_all_frames。
        batch_all_frames=True,
    )

    def compute(self, ctx):
        out = {}
        lam, M, t = ctx.lam, ctx.M, ctx.t
        lo, hi = ctx.param("band", BAND)
        absorb = bool(ctx.meta.get("input_is_absorbance", False))

        # ── 两张条纹图：服务端渲染成位图，前端配矢量坐标轴。
        #    条纹图**永远逐帧归一化** —— 要看的就是每一帧内部的明暗周期。
        #    位图本身是服务端按 URL 现渲的，这里只是拼地址，便宜。
        out["fringe_full"] = PanelData(
            image_url=ctx.image_url("heatmap.png", axis="wavenumber",
                                    norm="frame", cmap="gray"),
            x_range=[float(t[0]), float(t[-1])],
            y_range=[1 / float(lam[-1]), 1 / float(lam[0])],
            v_range=[0, 1], v_label="每帧归一化", cmap="gray",
            y_label="k = 1/λ (nm⁻¹)",
            # 「全波段」到底是哪一段，得跟着这份数据说出来 ——
            # 换一台光谱仪范围就变了，写死在标题里只对自己这台成立。
            stats=[Stat("波长范围", f"{lam[0]:.0f}–{lam[-1]:.0f}", "nm")],
            caption="全波段干涉条纹。相位对波数线性，所以只有在 k 轴上条纹才是等周期的")
        out["fringe_band"] = PanelData(
            image_url=ctx.image_url("heatmap.png", axis="wavenumber",
                                    norm="frame", cmap="gray",
                                    lam_min=f"{lo:g}", lam_max=f"{hi:g}"),
            x_range=[float(t[0]), float(t[-1])],
            y_range=[1 / float(hi), 1 / float(lo)],
            v_range=[0, 1], v_label="每帧归一化", cmap="gray",
            y_label="k = 1/λ (nm⁻¹)",
            # 窗口分辨率：Δk 决定能分辨的最小光程差。**纯几何量，不看数据。**
            # 先看一眼可以避免选一个根本测不出来的窗口 —— 选错时 FFT 会锁到
            # 噪声峰上，给出一个看起来很正常的错数。
            notice=Notice(
                "info",
                f"窗口 {lo:.0f}–{hi:.0f} nm　Δk = {_exp(_diag(lo, hi)['dk_range'])} nm⁻¹"
                f"　一个频率 bin = {_diag(lo, hi)['bin_f_nm']:.0f} nm",
                f"这个窗口能测的最小光学厚度约 {_diag(lo, hi)['ot_floor_nm']:.0f} nm"
                "（低于它 FFT 会锁到噪声峰）。窗口越宽，可测的膜越薄。"),
            caption="只看选定波段的条纹。窗口越窄，能分辨的膜越厚")

        # ── 两条 OT 曲线
        #
        # 全波段这一格 `uses=[]` —— 它不依赖任何控件，拖波段的时候结果不可能变。
        # 但它是一次全波段 FFT，实测 50 ms，占整次重算的四分之一。
        # `ctx.needs()` 让平台按声明告诉我们「这次不用算它」。
        if ctx.needs("ot_full"):
            out["ot_full"] = _ot_panel(lam, t, M,
                                       [float(lam[0]), float(lam[-1])], absorb)
            out["ot_full"].notice = Notice(
                "warn", "这一格是对照，不是测量结果",
                "全波段跨过了吸收边，还带上了短波端没有信号的区段 —— "
                "规范要求分析波段必须落在膜的透明区。这里画出来是为了跟下面那格比："
                "窗口选错时曲线会崩到噪声上，而且是看得见地崩 —— 不是悄悄给个错数。")

        band_panel = _ot_panel(lam, t, M, [lo, hi], absorb, want_report=True)
        report = band_panel.text
        band_panel.text = ""
        out["ot_band"] = band_panel
        out["report"] = PanelData(text=report)
        return out

    # `uses` 里没写控件的面板，平台就知道它跟控件无关。报告跟着指定波段走，
    # 所以它声明 uses=["band"]（见 spec）—— 那一格该重算的时候它也重算。


def _ot_panel(lam, t, M, window, absorb: bool, want_report: bool = False) -> PanelData:
    """一条 OT 曲线。

    ★ **标可信的那一段，不标不可信的。**

    上一版把不可信的帧画成红点。真实样品干燥后半段大半都不可信，
    整张图被红点糊满，反倒看不见「哪一段能用」—— 而那才是你看这张图的目的。
    现在反过来：底下一条淡灰的完整曲线（数值一个不藏，**判据只打标志、
    绝不修改数值**），可信的那一段用实色压在上面。
    """
    try:
        res = fringe_ot.extract_series(
            lam, t, M,
            target_times_s="all",
            window_nm=[float(window[0]), float(window[1])],
            # 「算得准」的门槛按平台的实际样品调到 2 条纹（规范 DEFAULTS 是 3）。
            # 显式 override，DEFAULTS 不动 —— 报告块 A 回显的是这里传的值。
            accurate_cycles=fringe_ot.PLATFORM_ACCURATE_CYCLES,
            input_is_absorbance=absorb,
        )
    except fringe_ot.FringeError as exc:
        # 算不出来时也把 ot_ok 补齐成全 0（一帧都不可信）。
        # 少这一列的话批处理只会报「模块没返回 ot_ok」—— 那句话把真正的原因
        # （窗口不对）盖掉了，而下面这句 caption 才是要看的。
        return PanelData(x=list(t), y=[None] * len(t),
                         caption=f"这个窗口算不出来：{exc}",
                         batch_extra={"ot_ok": [0.0] * len(t)})

    pts = res["points"]
    xs = [round(q["t"], 4) for q in pts]
    ys = [round(q["ot_nm"], 3) for q in pts]
    status = [q["status"] for q in pts]

    ok_x, ok_y, deg_x, deg_y = [], [], [], []
    for x, y, st in zip(xs, ys, status):
        if st == "OK":
            ok_x.append(x); ok_y.append(y)
        elif st == "DEGRADED":
            deg_x.append(x); deg_y.append(y)

    series = [Series(xs, ys, "全部帧（含不可信）", "line", "muted")]
    if deg_x:
        series.append(Series(deg_x, deg_y, "可用（精度下降）", "scatter", "warn"))
    if ok_x:
        series.append(Series(ok_x, ok_y, "可信", "scatter", "ok"))

    diag = res["diagnostics"]
    counts: dict[str, int] = {}
    for st in status:
        counts[st] = counts.get(st, 0) + 1
    n_ok = counts.get("OK", 0)

    stats = [
        Stat("共", len(pts), "帧"),
        Stat("可信", n_ok, "帧", tone="ok" if n_ok else "warn"),
    ]
    if counts.get("DEGRADED"):
        stats.append(Stat("可用但精度下降", counts["DEGRADED"], "帧", tone="warn"))
    stats += [
        Stat("可测下限", round(diag["ot_floor_nm"]), "nm", info="ot_floor"),
        Stat("量化格距", round(diag["ot_quantum_nm"]), "nm", info="ot_quantum"),
    ]

    # 这张图上出现过的判级，各配一句人话 + 本图里多少帧。
    # 文案来自 fringe_ot.explain_status，前后端不各写一份。
    items = []
    for st in sorted(counts):
        e = fringe_ot.explain_status(st)
        items.append({"key": st, "label": e["label"],
                      "note": e.get("detail") or e.get("short", ""),
                      "count": counts[st]})

    d = PanelData(
        series=series, stats=stats,
        y_label="光学厚度 OT = n·d·cosθ (nm)",
        info_extra={"title": "这张图上出现过的判级", "items": items},
        # 每帧可不可信。不画在图上，但批处理要拿它算「这个时间窗里几帧可信」。
        # 没有它的话批处理只能自己再跑一遍 FFT —— 同一件事两份实现，
        # 迟早对不上，而且没人知道该信哪个。
        batch_extra={"ot_ok": [1.0 if st == "OK" else 0.0 for st in status]},
    )
    if want_report:
        # §5 要求块 A–D 全文，禁止简化、禁止省略。
        # **max_rows=0 = 逐帧全列** —— 抽样过的报告看不出干燥在哪一秒变坏。
        d.text = fringe_ot.format_report(res, max_rows=0)
    return d


def _exp(v: float) -> str:
    """3.975e-04 → 3.975e-4。Python 补的两位指数在图上看着像个错别字。"""
    return f"{v:.3e}".replace("e-0", "e-").replace("e+0", "e+")


def _diag(lo: float, hi: float) -> dict:
    """窗口分辨率。纯几何量，只跟窗口有关，所以按窗口缓存 ——
    一次重算里要问三次，没必要算三遍。"""
    key = (round(float(lo), 6), round(float(hi), 6))
    if key not in _DIAG_CACHE:
        _DIAG_CACHE[key] = fringe_ot.diagnostics_for(*key)
    return _DIAG_CACHE[key]


_DIAG_CACHE: dict[tuple[float, float], dict] = {}


MODULE = Thickness()
