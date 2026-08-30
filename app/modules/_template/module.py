"""模块模板 —— 复制这个目录，改个名字，就能开始写。

    cp -r workspace/modules/_template workspace/modules/我的模块

改完打开「设置 → 功能模块」点「重载」。装不上的话，验证器会逐条告诉你
哪个字段错在哪 —— 把那段报错贴给你的模型，让它改，再试一次。

完整说明见 docs/MODULE_AUTHORING.md。
下划线开头的目录不会被加载，所以这个模板本身不会出现在界面上。
"""
from app.modules.base import Control, Curve, Module, ModuleSpec, Op, Panel


class MyModule(Module):
    spec = ModuleSpec(
        # ── 身份。id 全局唯一，小写 + 点分段；version 会跟着结果落库
        id="my.module",
        name="我的模块",
        version="1.0.0",
        description="一句话说清这个模块算什么",
        author="你的名字",

        # ── 控件：用户能调的参数
        controls=[
            Control("band", "波段", "band", default=[800, 950], unit="nm",
                    help="鼠标悬停时显示的说明"),
            # 波长类的数字控件让上下限跟着数据走，别写死 max
            # Control("center", "波长", "number", default=950, unit="nm",
            #         range_from="lambda", step=1),
        ],

        # ── 面板：一格图
        #
        #    有 live=  → A 档：拖控件时在浏览器里实时算（2 ms 级）
        #    没有 live= → B 档：写下面的 compute()，松手才算
        #
        #    现有算子只有两个：
        #      Op.band_integral(band="某个 band 控件")
        #      Op.wavelength_slope(center="某个 number 控件", half="某个 number 控件")
        panels=[
            Panel("main", "我的曲线 vs 时间",
                  uses=["band"],              # 控件画在这一格上面
                  live=Op.band_integral(band="band"),
                  info="integral"),           # 标题旁的 ⓘ，填术语表里的 id；不要就删掉
        ],

        # ── 批处理：声明了，你的曲线就自动进长表、对比页叠图、
        #    时刻切片和导出脚本。不声明就只在单样品页显示。
        batch_curves=[Curve("my_curve", from_panel="main")],
        batch_metrics=[],
    )

    # ── B 档才需要这个方法。纯 A 档模块整段删掉即可。
    #
    # def compute(self, ctx):
    #     import numpy as np
    #     from app.modules.base import PanelData
    #
    #     out = super().compute(ctx)      # ← A 档面板归基类管，先拿到手
    #
    #     # ctx.lam  波长轴 (n_lambda,)
    #     # ctx.M    矩阵   (n_lambda, n_time)
    #     # ctx.t    时间轴 (n_time,)
    #     # ctx.param("band") 取控件值
    #     lo, hi = ctx.param("band")
    #     mask = (ctx.lam >= lo) & (ctx.lam <= hi)
    #     y = ctx.M[mask].max(axis=0)     # y 必须和 ctx.t 一样长
    #
    #     out["main"] = PanelData(x=list(ctx.t), y=list(y), y_label="峰值强度")
    #     return out


MODULE = MyModule()          # 别忘了这一行 —— 平台靠它找到你的模块
