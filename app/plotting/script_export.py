"""导出可以自己跑的绘图脚本。

**为什么是脚本而不是让模型直接画：** 平台内置图型不够用时，你拿脚本走人
随便改；而且论文里那张图是你自己能读、能改、能引用的代码画的 ——
不是模型在某个沙箱里跑出来的黑箱。

脚本里的 rcParams / COLORS / MARKERS / LINESTYLES 逐字来自
Mengnan 的 matplotlib 规范，不是"差不多"的版本。
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any

# ---- 以下四块逐字对应 matplotlib-mengnan-style skill，改动前先看那份规范 ----
RCPARAMS = """plt.rcParams.update({
    "font.size": 15,
    "axes.linewidth": 2,
    "axes.labelsize": 15,
    "axes.titlesize": 15,
    "xtick.labelsize": 13,
    "ytick.labelsize": 13,
    "xtick.major.width": 2,
    "ytick.major.width": 2,
    "xtick.minor.width": 1.5,
    "ytick.minor.width": 1.5,
    "xtick.direction": "in",
    "ytick.direction": "in",
    "xtick.major.size": 6,
    "ytick.major.size": 6,
    "xtick.minor.size": 3,
    "ytick.minor.size": 3,
    "lines.linewidth": 2,
    "lines.markersize": 13,
    "lines.markeredgewidth": 2,
    "legend.fontsize": 10,
    "legend.markerscale": 1.5,
    "legend.framealpha": 0.5,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
})"""

PALETTE = '''COLORS = [
    "#2470a0",  # 1  muted blue
    "#ca3e47",  # 2  muted red
    "#f29c2b",  # 3  muted orange
    "#1f640a",  # 4  dark green
    "#2ca02c",  # 5  asparagus green
    "#9467bd",  # 6  muted purple
    "#8c564b",  # 7  chestnut brown
    "#e377c2",  # 8  raspberry pink
    "#7f7f7f",  # 9  middle gray
    "#bcbd22",  # 10 curry yellow-green
    "#17becf",  # 11 blue-teal
    "#005555",  # 12 FHI green
]

MARKERS = ["o", "v", "s", "*", "p", "P", ","]

LINESTYLES = [
    "-",
    "--",
    "-.",
    ":",
    (0, (3, 5, 1, 5, 1, 5)),
    (0, (5, 5)),
]'''


FONT_BLOCK = """# 中文标签要有中文字体。挑一个这台机器上真的装了的；一个都没有就退回
# 英文标签 —— 画出一排豆腐块比英文难看得多，而且看不出是哪条曲线。
_CJK_FONTS = ["Microsoft YaHei", "SimHei", "PingFang SC", "Heiti SC",
              "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei"]
_installed = {f.name for f in font_manager.fontManager.ttflist}
_cjk = next((f for f in _CJK_FONTS if f in _installed), None)
if _cjk:
    plt.rcParams["font.sans-serif"] = [_cjk] + list(plt.rcParams["font.sans-serif"])
    plt.rcParams["axes.unicode_minus"] = False   # 中文字体的减号常常是缺的
CJK_OK = _cjk is not None


def L(zh, en):
    \"\"\"有中文字体就用中文，没有就用英文。\"\"\"
    return zh if CJK_OK else en
"""


def _title_line(title: str, title_en: str) -> str:
    if not title:
        return '# ax.set_title("...")'
    return f"ax.set_title(L({title!r}, {title_en or title!r}))"


def build_script(*, column: str, y_label: str, y_label_en: str, mode: str,
                 group_by: str, n_series: int, title: str = "",
                 title_en: str = "") -> str:
    """生成 plot.py。

    mode='overlay' 逐条叠图，mode='band' 中位数 + 四分位带。

    每个标签都要一份英文：脚本会被拷到别的机器上跑，那台机器不一定装了
    中文字体。宁可退回英文，也不要画出一排豆腐块。
    """
    figsize = "(7, 4)" if mode == "overlay" and n_series > 6 else "(5, 4)"
    body = _overlay_body(group_by) if mode == "overlay" else _band_body()

    return f'''"""跨样品叠图 —— 由 HTE Studio 导出。

数据在同目录的 data.csv 里，长表格式：
    sample_id, sample_name, batch, label, t, {column}

一条曲线 = 一个 sample_id。**不要按 sample_name 分组** —— 样品的身份是
(名字, 批次)，S1 在每个批次里都有一个，只按名字会把它们悄悄合成一条。

样式遵循 Mengnan 的 matplotlib 规范（rcParams / 12 色序列 / 标记 / 线型）。
改这个脚本比改平台容易 —— 它就是给你改的。

    python plot.py            # 出 figure.png（300 dpi）
    python plot.py --show     # 顺便弹窗看
"""
import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

# ---------------------------------------------------------------- 样式
{RCPARAMS}

{PALETTE}

{FONT_BLOCK}

# ---------------------------------------------------------------- 数据
HERE = Path(__file__).parent
df = pd.read_csv(HERE / "data.csv")

Y_COLUMN = {column!r}
Y_LABEL = L({y_label!r}, {y_label_en!r})
X_LABEL = L("时间 (s)", "Time (s)")
GROUP_BY = {group_by!r}          # "batch" 按批次着色，"none" 逐条不同色

fig, ax = plt.subplots(figsize={figsize})

{body}

ax.set_xlabel(X_LABEL)
ax.set_ylabel(Y_LABEL)
{_title_line(title, title_en)}
ax.tick_params(which="both", direction="in", top=True, right=True)

if ax.get_legend_handles_labels()[0]:
    ax.legend(frameon=True)

fig.tight_layout()

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--show", action="store_true", help="除了存文件还弹窗显示")
    p.add_argument("-o", "--out", default="figure.png")
    args = p.parse_args()
    fig.savefig(HERE / args.out)
    print(f"已保存 {{HERE / args.out}}")
    if args.show:
        plt.show()
'''


def _overlay_body(group_by: str) -> str:
    if group_by == "batch":
        return '''# 按批次着色。超过 12 组的话颜色会循环 —— 那时候更该考虑分位数带。
groups = list(dict.fromkeys(df["batch"].fillna("")))
for gi, g in enumerate(groups):
    sub = df[df["batch"].fillna("") == g]
    color = COLORS[gi % len(COLORS)]
    for si, (_, s) in enumerate(sub.groupby("sample_id", sort=False)):
        ax.plot(s["t"], s[Y_COLUMN],
                color=color,
                linestyle=LINESTYLES[0],
                linewidth=2,
                alpha=0.85,
                zorder=2,
                label=str(g) if si == 0 else None)'''
    return '''# 逐条不同色。曲线多了会看不清，这时候用 --band 那份脚本更合适。
for i, (_sid, s) in enumerate(df.groupby("sample_id", sort=False)):
    ax.plot(s["t"], s[Y_COLUMN],
            color=COLORS[i % len(COLORS)],
            linestyle=LINESTYLES[i % len(LINESTYLES)],
            linewidth=2,
            alpha=0.85,
            zorder=2,
            label=str(s["label"].iloc[0]))'''


def _band_body() -> str:
    return '''# 中位数 + 四分位带。
# 上千条曲线叠一张图是噪声不是图 —— 这里把它压成一条统计曲线，
# 再叠几条代表曲线，让人还能看到个体形状。
grid = np.linspace(df["t"].min(), df["t"].max(), 240)
curves = []
for _, s in df.groupby("sample_id", sort=False):
    curves.append(np.interp(grid, s["t"], s[Y_COLUMN], left=np.nan, right=np.nan))
stack = np.vstack(curves)

median = np.nanmedian(stack, axis=0)
q1 = np.nanpercentile(stack, 25, axis=0)
q3 = np.nanpercentile(stack, 75, axis=0)

ax.fill_between(grid, q1, q3,
                color=COLORS[0], alpha=0.20, linewidth=0, zorder=1,
                label=L("四分位区间", "IQR"))
ax.plot(grid, median,
        color=COLORS[0], linestyle=LINESTYLES[0], linewidth=2.5,
        zorder=3, label=L(f"中位数（n={len(curves)}）", f"median (n={len(curves)})"))

# 叠几条代表曲线
step = max(1, len(curves) // 5)
sids = list(dict.fromkeys(df["sample_id"]))
for i, sid in enumerate(sids[::step][:5]):
    s = df[df["sample_id"] == sid]
    ax.plot(s["t"], s[Y_COLUMN],
            color=COLORS[(i + 1) % len(COLORS)],
            linestyle=LINESTYLES[0], linewidth=1.2, alpha=0.55, zorder=2)'''


README = """# {title}

由 HTE Studio 导出。

## 里面是什么

- `plot.py`   绘图脚本，样式已按 matplotlib 规范设好
- `data.csv`  长表数据：`sample_id, sample_name, batch, label, t, {column}`

## 怎么跑

```bash
pip install matplotlib pandas numpy
python plot.py            # 出 figure.png（300 dpi）
python plot.py --show     # 顺便弹窗
```

## 数据说明

- {n_samples} 个样品，{n_rows} 行
- 曲线：{y_label}
- 配方：{recipe}
- 来自批处理 `{run_id}`

## 为什么给的是脚本

平台内置的图型不够用时，你拿这个脚本走人随便改。而且论文里那张图是你自己
能读、能改、能引用的代码画的 —— 不是模型在某个沙箱里跑出来的黑箱。

改样式看 `plot.py` 顶部的 `rcParams` / `COLORS` / `MARKERS` / `LINESTYLES`，
它们逐字对应你的规范。
"""


def build_zip(*, csv_bytes: bytes, column: str, y_label: str, mode: str,
              group_by: str, n_series: int, n_rows: int, recipe: dict,
              run_id: str, y_label_en: str = "", title: str = "跨样品叠图",
              title_en: str = "") -> bytes:
    """打包成 zip：plot.py + data.csv + README.md"""
    script = build_script(column=column, y_label=y_label,
                          y_label_en=y_label_en or y_label, mode=mode,
                          group_by=group_by, n_series=n_series,
                          title=title, title_en=title_en or title)
    readme = README.format(
        title=title, column=column, n_samples=n_series, n_rows=n_rows,
        y_label=y_label, run_id=run_id,
        recipe=", ".join(f"{k}={v}" for k, v in (recipe or {}).items()) or "（默认）")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("plot.py", script)
        z.writestr("data.csv", csv_bytes)
        z.writestr("README.md", readme)
    return buf.getvalue()
