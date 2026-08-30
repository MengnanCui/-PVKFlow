"""特殊处理 —— 谱斜率 + 波段积分。

这个模块是**契约的活示范**：平台自己的一个功能，完整地走了一遍
「同事怎么加功能」那条路。要照着写一个新模块，复制这个文件改就行。

值得注意的是下面**一行算法都没有**。两个面板都是 A 档 ——
用平台的算子拼出来的，所以拖控件时在浏览器里实时算（2 ms 级），
批处理时在后端用同一份 Python 实现算。两份实现由 tests/test_ops.py 钉住数值一致。

要写自己的算法（峰拟合、FFT、任意 numpy）就加一个 `compute()` 方法，
那是 B 档：参数松手才重算。见 docs/MODULE_AUTHORING.md。
"""
from app.modules.base import Control, Curve, Module, ModuleSpec, Op, Panel


class SpecialProcessing(Module):
    spec = ModuleSpec(
        id="special.slope_integral",
        name="特殊处理",
        version="1.0.0",
        description="谱斜率与波段积分随时间的变化。两条曲线都用平台算子实时计算。",
        author="HTE Studio",
        order=20,

        # ── 控件。平台渲染成功能块里的输入框和滑块，样式不归模块管。
        #    波段控件的上下限自动跟着数据走，这里只给默认值。
        controls=[
            # 上下限跟着这份数据的波长轴走 —— 写死一个数只对一台光谱仪成立
            Control("slope_center", "波长", "number", default=950, unit="nm",
                    range_from="lambda", step=1,
                    help="在这个波长附近取窗口做线性拟合"),
            Control("slope_half", "半宽", "number", default=10, unit="nm",
                    min=1, max=200, step=1,
                    help="窗口太窄会被噪声主导，太宽会把曲率算进来"),
            Control("integ", "波段", "band", default=[800, 950], unit="nm",
                    help="对这一段的强度积分"),
        ],

        # ── 面板。一格图。左右并排、三行结构、等高对齐由平台保证。
        #    有 live= 就是 A 档（能拖）。
        panels=[
            Panel("slope", "谱斜率 vs 时间",
                  uses=["slope_center", "slope_half"],
                  live=Op.wavelength_slope(center="slope_center", half="slope_half"),
                  info="slope"),
            Panel("integ", "波段积分 vs 时间",
                  uses=["integ"],
                  live=Op.band_integral(band="integ"),
                  info="integral"),
        ],

        # ── 批处理。声明了这些，平台就自动把它接进长表、对比页叠图、
        #    时刻切片和导出脚本 —— 模块不用碰 app/batch.py。
        batch_curves=[
            Curve("slope", from_panel="slope"),
            Curve("integral", from_panel="integ"),
        ],
        batch_metrics=["integral_initial", "integral_final",
                       "integral_ratio", "slope_abs_max"],
    )


MODULE = SpecialProcessing()
