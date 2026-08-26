"""通用表格解析 —— 让任何导入的数据文件立刻有东西可看。

这是唯一一个「什么都吃」的 skill：嗅探编码/分隔符/抬头，读成表，
出列统计与一张 X-Y 图。它的存在保证了平台在你接入专用 skill 之前
就不是空的。

它也是 SKILL_CONTRACT 的活样例——想知道一个 skill 长什么样，看这个文件。
"""
from __future__ import annotations

from app.skills.base import (
    ChartSpec, FileMatch, Metric, OutputSpec, ParamSpec, Skill, SkillContext,
    SkillResult, SkillSpec,
)


class TablePreviewSkill(Skill):
    spec = SkillSpec(
        id="table.preview",
        name="通用表格解析",
        category="table",
        version="1.0.0",
        description=(
            "自动识别编码、分隔符、抬头行与列类型，把任意 csv/txt/dat/xlsx 读成表格，"
            "给出每列的统计量，并按选定的 X/Y 列出图。"
        ),
        accepts=FileMatch(
            extensions=[".csv", ".txt", ".dat", ".tsv", ".asc", ".xy", ".xlsx", ".xls"],
            max_files=1,
        ),
        params=[
            ParamSpec("x_column", "X 轴列", "column", default=None,
                      help="留空则用第一个数值列"),
            ParamSpec("y_columns", "Y 轴列", "columns", default=None,
                      help="留空则用其余全部数值列（最多 6 条）"),
            ParamSpec("max_rows", "最多读取行数", "number", default=200000,
                      min=100, step=1000,
                      help="超大文件时限制读取量，避免界面卡住"),
        ],
        outputs=[
            OutputSpec("n_rows", "数据行数", kind="number"),
            OutputSpec("n_columns", "列数", kind="number"),
            OutputSpec("columns", "列名", kind="text"),
        ],
    )

    def run(self, ctx: SkillContext) -> SkillResult:
        import numpy as np
        import pandas as pd

        max_rows = int(ctx.param("max_rows", 200000) or 200000)
        df, sniffed = ctx.load_table(max_rows=max_rows)
        ctx.logline(f"编码 {sniffed.encoding}，分隔符 {sniffed.delimiter!r}，"
                    f"表头行 {sniffed.header_row}")
        if getattr(sniffed, "preamble", None):
            ctx.logline(f"抬头 {len(sniffed.preamble)} 行（已跳过）")

        numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        warnings: list[str] = []
        if df.empty:
            warnings.append("没有读到任何数据行")
        if not numeric_cols:
            warnings.append("没有识别到数值列，只能做文本预览")

        # -------- 每列统计，写成关键结果 --------
        metrics = [
            Metric("n_rows", int(len(df)), label="数据行数", quality="validated"),
            Metric("n_columns", int(df.shape[1]), label="列数", quality="validated"),
            Metric("columns", ", ".join(str(c) for c in df.columns), label="列名",
                   quality="validated"),
        ]
        for col in numeric_cols[:12]:
            s = pd.to_numeric(df[col], errors="coerce")
            if s.notna().sum() == 0:
                continue
            metrics.append(Metric(f"{col}__min", float(np.nanmin(s)), label=f"{col} 最小值"))
            metrics.append(Metric(f"{col}__max", float(np.nanmax(s)), label=f"{col} 最大值"))
            metrics.append(Metric(f"{col}__mean", float(np.nanmean(s)), label=f"{col} 平均"))

        # -------- 数据质量提示 --------
        nan_ratio = float(df.isna().sum().sum()) / max(1, df.size)
        if nan_ratio > 0.2:
            warnings.append(f"缺失值占比 {nan_ratio:.0%}，检查一下分隔符是否识别正确")
        dup = int(df.duplicated().sum())
        if dup:
            warnings.append(f"有 {dup} 行完全重复")

        # -------- 出图 --------
        preview = None
        if numeric_cols:
            x = ctx.param("x_column") or numeric_cols[0]
            if x not in df.columns:
                x = numeric_cols[0]
            ys = ctx.param("y_columns")
            if isinstance(ys, str):
                ys = [y.strip() for y in ys.split(",") if y.strip()]
            if not ys:
                ys = [c for c in numeric_cols if c != x][:6]
            ys = [c for c in ys if c in df.columns]
            if ys:
                preview = ChartSpec.from_frame(df, x=x, ys=ys, x_label=str(x), style="line")

        return SkillResult(
            metrics=metrics,
            tables={"data": df},
            preview=preview,
            summary=(f"{len(df)} 行 × {df.shape[1]} 列，"
                     f"其中数值列 {len(numeric_cols)} 个：{', '.join(map(str, numeric_cols[:6]))}"
                     + ("…" if len(numeric_cols) > 6 else "")),
            warnings=warnings,
            logs=ctx.log_text,
            extra={"sniffed": sniffed.as_dict() if hasattr(sniffed, "as_dict") else {}},
        )


SKILL = TablePreviewSkill()
