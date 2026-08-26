"""光谱 —— 接口已就位，算法留给你填。

═══════════════════════════════════════════════════════════════════════════
 怎么把你现成的光谱算法接进来（只需要动两个地方）：

   1. 把 `ALGORITHM_READY` 改成 True
   2. 实现下面的 `analyze(df, params, ctx)`，返回一个 dict：
          {"bandgap": 1.58, "transmittance_avg": 82.4, "peak_wavelength": 780}
      键名要和 spec.outputs 里声明的 field_name 对上。

 平台已经处理好：文件嗅探、参数表单、结果卡片、出图、落库与版本追溯。
 spec.params / spec.outputs 按你算法的真实需要改，界面会跟着变。
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
    """★ 在这里放你的光谱算法。

    参数
    ----
    df : pandas.DataFrame
        已解析好的谱图数据，通常是「波长 / 强度」两列或多列。
    params : dict
        界面填的参数，键是 spec.params 里的 key。
    ctx : SkillContext
        `ctx.path` 原始文件路径；`ctx.sniff()` 拿嗅探细节（含仪器抬头）；
        `ctx.logline()` 写日志。

    返回
    ----
    dict —— 键对应 spec.outputs 里的 field_name。
    """
    raise NotImplementedError(
        "光谱算法还没有接入。\n"
        "请编辑 app/skills/builtin/spectrum/skill.py：\n"
        "  1) 实现 analyze()\n"
        "  2) 把文件顶部的 ALGORITHM_READY 改成 True"
    )


class SpectrumSkill(Skill):
    spec = SkillSpec(
        id="spectrum.generic",
        name="光谱",
        category="spectrum",
        version="0.1.0",
        description=(
            "UV-Vis / PL / 反射透射谱的特征提取。接口与存储链路已就位，"
            "算法接入见 app/skills/builtin/spectrum/skill.py。"
        ),
        accepts=FileMatch(
            extensions=[".csv", ".txt", ".dat", ".asc", ".spc", ".xy", ".xlsx"],
            filename_globs=["*spec*", "*光谱*", "*uvvis*", "*uv-vis*", "*pl*", "*abs*", "*trans*"],
            content_keywords=["wavelength", "nm", "absorbance", "transmittance", "intensity"],
            max_files=1,
        ),
        params=[
            ParamSpec("wavelength_column", "波长列", "column", default=None,
                      help="留空则自动取第一个数值列"),
            ParamSpec("signal_column", "信号列", "column", default=None,
                      help="留空则自动取第二个数值列"),
            ParamSpec("range", "分析波段", "range", default=[300, 900], unit="nm"),
            ParamSpec("mode", "谱类型", "select", default="transmittance",
                      options=["transmittance", "absorbance", "reflectance", "PL"]),
        ],
        outputs=[
            OutputSpec("bandgap", "光学带隙", unit="eV"),
            OutputSpec("transmittance_avg", "平均透过率", unit="%"),
            OutputSpec("peak_wavelength", "峰位", unit="nm"),
        ],
        ready=ALGORITHM_READY,
        ready_note=(
            "契约与存储链路已就位，光谱算法尚未接入。"
            "编辑 app/skills/builtin/spectrum/skill.py 里的 analyze() 即可启用。"
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

        preview = None
        numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        if len(numeric) >= 2:
            x = ctx.param("wavelength_column") or numeric[0]
            y = ctx.param("signal_column") or numeric[1]
            if x in df.columns and y in df.columns:
                preview = ChartSpec.from_frame(
                    df, x=x, ys=[y], x_label=str(x), y_label=str(y))

        return SkillResult(
            metrics=metrics, tables={"spectrum": df}, preview=preview, logs=ctx.log_text,
        )


SKILL = SpectrumSkill()
