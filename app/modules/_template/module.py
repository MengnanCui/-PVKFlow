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
            # 除了曲线，还有两种面板（要用再解开）：
            # Panel("fringe", "热力图", kind="heatmap"),   # 服务端出位图 + 矢量坐标轴
            # Panel("report", "完整报告", kind="text", span=1),  # 整幅宽的等宽文本
        ],

        # ── 批处理：声明了，你的曲线就自动进长表、对比页叠图、
        #    时刻切片和导出脚本。不声明就只在单样品页显示。
        batch_curves=[Curve("my_curve", from_panel="main")],
        batch_metrics=[],
        # 算出来但不画在图上、又该进批处理的数字，走 PanelData.batch_extra：
        # batch_curves=[Curve("my_curve", from_panel="main"),
        #               Curve("my_flag", from_panel="main", key="my_flag")],
        #
        # 逐帧的量（比如膜厚）不能被 max_time_points 抽稀，加这一句。
        # 代价是多算一遍全部帧，贵的才开。
        # batch_all_frames=True,
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
    #
    # ── 一格里画多条线、加图注和提示块（要用再解开）：
    #
    #     from app.modules.base import Notice, Series, Stat
    #
    #     PanelData(
    #         series=[Series(ctx.t, y_all, "全部帧", "line", "muted"),
    #                 Series(ok_t, ok_y,   "可信",   "scatter", "ok")],
    #         # 颜色只收语义名：auto / ok / warn / danger / muted / accent
    #         # 不收十六进制 —— 那样明暗两套主题下就不对了
    #         stats=[Stat("可信", 83, "帧", tone="ok"),
    #                Stat("可测下限", 351, "nm", info="ot_floor")],
    #         notice=Notice("warn", "这一格是对照", "不是测量结果"),
    #         info_extra={"title": "这张图上出现过的判级", "items": [...]},
    #         batch_extra={"my_flag": [1.0, 0.0, ...]},   # 不画、但进批处理
    #     )
    #
    # ── 贵的活别白算：uses=[] 的面板，拖别的控件时结果不可能变
    #
    #     if ctx.needs("某个面板 id"):
    #         out["某个面板 id"] = 很贵的计算(...)
    #
    #   不写也不会算错，只是慢一点。这是优化，不是义务。


MODULE = MyModule()          # 别忘了这一行 —— 平台靠它找到你的模块
