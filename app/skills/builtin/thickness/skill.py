"""膜厚 —— 接口已就位，算法留给你填。

═══════════════════════════════════════════════════════════════════════════
 怎么把你现成的膜厚算法接进来（只需要动两个地方）：

   1. 把 `ALGORITHM_READY` 改成 True
   2. 实现下面的 `analyze(df, params, ctx)`，返回一个 dict：
          {"thickness": 812.4, "roughness": 3.1, "step_count": 2}
      键名要和 spec.outputs 里声明的 field_name 对上。

 其余的事情平台已经做完了：
   * 文件的编码 / 分隔符 / 仪器抬头 —— ctx.load_table() 已经嗅探好
   * 参数表单 —— 按 spec.params 自动渲染，不用碰前端
   * 结果卡片 —— 按 spec.outputs 自动渲染
   * 曲线图 —— 返回 ChartSpec 就会画出来
   * 落库 —— analysis_run + key_result + Parquet 由 runner 统一处理

 spec.params / spec.outputs 按你算法的真实需要改就行，界面会跟着变。
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from typing import Any

from app.skills.base import (
    ChartSpec, FileMatch, Metric, OutputSpec, ParamSpec, Skill, SkillContext,
    SkillResult, SkillSpec,
)

# 把这里改成 True，这个 skill 就会从「待接入」变成可运行
ALGORITHM_READY = False


def analyze(df, params: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
    """★ 在这里放你的膜厚算法。

    参数
    ----
    df : pandas.DataFrame
        已经解析好的数据表。仪器抬头、编码、分隔符都处理过了。
        列名就是文件里的列名，用 `ctx.sniff()` 可以拿到嗅探细节。
    params : dict
        界面上填的参数，键是 spec.params 里的 key。
    ctx : SkillContext
        `ctx.path` 是原始文件路径（需要自己解析二进制格式时用），
        `ctx.logline("…")` 写运行日志，会存进 analysis_run.log。

    返回
    ----
    dict —— 键对应 spec.outputs 里的 field_name，值是数字或字符串。
    """
    raise NotImplementedError(
        "膜厚算法还没有接入。\n"
        "请编辑 app/skills/builtin/thickness/skill.py：\n"
        "  1) 实现 analyze()\n"
        "  2) 把文件顶部的 ALGORITHM_READY 改成 True"
    )


class ThicknessSkill(Skill):
    spec = SkillSpec(
        id="thickness.generic",
        name="膜厚",
        category="thickness",
        version="0.1.0",
        description=(
            "台阶仪 / 椭偏 / 轮廓曲线的膜厚提取。接口与存储链路已就位，"
            "算法接入见 app/skills/builtin/thickness/skill.py。"
        ),
        accepts=FileMatch(
            extensions=[".csv", ".txt", ".dat", ".asc", ".xlsx"],
            filename_globs=["*thick*", "*膜厚*", "*profil*", "*step*", "*ellips*"],
            content_keywords=["thickness", "profile", "step height", "height", "position"],
            max_files=1,
        ),
        params=[
            # 这些是占位示例，按你的算法真实需要改
            ParamSpec("x_column", "位置列", "column", default=None,
                      help="留空则自动取第一个数值列"),
            ParamSpec("y_column", "高度列", "column", default=None,
                      help="留空则自动取第二个数值列"),
            ParamSpec("baseline", "基线区间", "range", default=[0, 10], unit="%",
                      help="用曲线两端多长的区间拟合基线"),
            ParamSpec("smooth", "平滑窗口", "number", default=5, min=0, step=1,
                      help="0 表示不平滑"),
        ],
        outputs=[
            OutputSpec("thickness", "膜厚", unit="nm"),
            OutputSpec("roughness", "粗糙度", unit="nm"),
            OutputSpec("step_count", "台阶数", kind="number"),
        ],
        ready=ALGORITHM_READY,
        ready_note=(
            "契约与存储链路已就位，膜厚算法尚未接入。"
            "编辑 app/skills/builtin/thickness/skill.py 里的 analyze() 即可启用。"
        ),
    )

    def run(self, ctx: SkillContext) -> SkillResult:
        import pandas as pd

        df, sniffed = ctx.load_table()
        ctx.logline(f"读入 {len(df)} 行 × {df.shape[1]} 列")

        values = analyze(df, ctx.params, ctx)      # ← 你的算法
        if not isinstance(values, dict):
            raise TypeError(f"analyze() 应该返回 dict，实际返回了 {type(values).__name__}")

        declared = {o.field_name: o for o in self.spec.outputs}
        metrics = []
        for name, value in values.items():
            o = declared.get(name)
            metrics.append(Metric(
                field_name=name, value=value,
                unit=(o.unit if o else ""), label=(o.label if o else name),
            ))

        # 把原始轮廓画出来，方便人眼复核算法结果
        preview = None
        numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        if len(numeric) >= 2:
            x = ctx.param("x_column") or numeric[0]
            y = ctx.param("y_column") or numeric[1]
            if x in df.columns and y in df.columns:
                preview = ChartSpec.from_frame(df, x=x, ys=[y], x_label=str(x), y_label=str(y))

        return SkillResult(
            metrics=metrics, tables={"profile": df}, preview=preview,
            logs=ctx.log_text,
        )


SKILL = ThicknessSkill()
