"""出图。样式逐字照 Mengnan 的 matplotlib 规范（rcParams / COLORS / 直方图黑边 / 折线 alpha），
只把保存分辨率从 300 dpi 降到 110 dpi —— 这些是网页素材，一次处理要出几百张（D-174）。

图里的文字一律英文 + 编号（E1、E2……）：服务器上不一定有中文字体，中文会变成方框；
编号和实验名的对照写在 summary.json 和网页上。

matplotlib 用面向对象的 Figure + Agg，不碰 pyplot；样式靠 rc_context（会改全局 rcParams），所以出图整段在 _RC_LOCK 里。
"""
from __future__ import annotations

import math
import threading
from pathlib import Path

import numpy as np

COLORS = ["#2470a0", "#ca3e47", "#f29c2b", "#1f640a", "#2ca02c", "#9467bd",
          "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#005555"]
LINESTYLES = ["-", "--", "-.", ":", (0, (3, 5, 1, 5, 1, 5)), (0, (5, 5))]
RC = {
    "font.size": 15, "axes.linewidth": 2, "axes.labelsize": 15, "axes.titlesize": 15,
    "xtick.labelsize": 13, "ytick.labelsize": 13,
    "xtick.major.width": 2, "ytick.major.width": 2, "xtick.minor.width": 1.5, "ytick.minor.width": 1.5,
    "xtick.direction": "in", "ytick.direction": "in",
    "xtick.major.size": 6, "ytick.major.size": 6, "xtick.minor.size": 3, "ytick.minor.size": 3,
    "lines.linewidth": 2, "lines.markersize": 13, "lines.markeredgewidth": 2,
    "legend.fontsize": 10, "legend.markerscale": 1.5, "legend.framealpha": 0.5,
    "figure.dpi": 150, "savefig.dpi": 300, "savefig.bbox": "tight",
}
DPI = 110
CMAP = "viridis"
# rc_context 改的是进程全局的 rcParams：同时有两处在画（以后任务并行、或同事模块的 compute 也用 matplotlib）
# 就会串样式。画图的整段都在这把锁里（体检发现，C069）
_RC_LOCK = threading.Lock()


def _mpl():
    import matplotlib
    matplotlib.use("Agg", force=False)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    return matplotlib, Figure, FigureCanvasAgg


def _ticks(ax):
    ax.tick_params(axis="both", which="major", length=6, width=2, direction="in", labelsize=13)
    ax.tick_params(axis="both", which="minor", length=3, width=1.5, direction="in")


def _save(fig, path: Path) -> None:
    _, _, Canvas = _mpl()
    Canvas(fig)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI, bbox_inches="tight", pil_kwargs={"compress_level": 3})


def pool(a: np.ndarray, target: int = 600, how: str = "mean") -> tuple[np.ndarray, int]:
    """缩到长边不超过 target。数值取平均；掩膜取「有就算有」，细划痕不会缩没了。"""
    f = max(1, math.ceil(max(a.shape) / target))
    if f == 1:
        return a, 1
    H, W = a.shape[0] // f * f, a.shape[1] // f * f
    b = a[:H, :W].reshape(H // f, f, W // f, f)
    return (b.any(axis=(1, 3)) if how == "any" else b.mean(axis=(1, 3))), f


def _extent(shape, f, um):
    H, W = shape
    s = (um or 1.0)
    return [0, W * s, H * s, 0], ("x (µm)", "y (µm)") if um else ("x (px)", "y (px)")


# ------------------------------------------------------------------ 每个样点
def point_figures(out: Path, arr: np.ndarray, m: dict, extra: dict, title: str) -> list[str]:
    """heatmap / hist / profile / anomaly / variogram 五张 + summary（前四张拼起来）。返回文件名。"""
    matplotlib, Figure, _ = _mpl()
    out.mkdir(parents=True, exist_ok=True)
    um = m.get("pixel_size_um")
    small, f = pool(arr)
    ext, (xl, yl) = _extent(arr.shape, 1, um)
    lo, hi = m["p1"], m["p99"]
    made = []
    with _RC_LOCK, matplotlib.rc_context(RC):
        # 热图
        fig = Figure(figsize=(6, 4))
        ax = fig.add_subplot()
        im = ax.imshow(small, cmap=CMAP, vmin=lo, vmax=hi, extent=ext, interpolation="nearest")
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
        cb.set_label("QFLS (eV)")
        cb.ax.tick_params(labelsize=11, direction="in", width=1.5)
        ax.set_xlabel(xl)
        ax.set_ylabel(yl)
        ax.set_title(title, fontsize=11)
        _ticks(ax)
        _save(fig, out / "heatmap.png")
        made.append("heatmap.png")

        # 直方图
        fig = Figure(figsize=(5, 4))
        ax = fig.add_subplot()
        e = np.asarray(extra["hist"]["edges"])
        c = np.asarray(extra["hist"]["counts"], dtype=float)
        ax.bar((e[:-1] + e[1:]) / 2, c / c.sum(), width=np.diff(e), color=COLORS[0], alpha=0.75,
               edgecolor="black", linewidth=1.0, zorder=2)
        for v, ls, lab in ((m["median"], LINESTYLES[0], "median"),
                           (m["low_threshold"], LINESTYLES[1], "median ± 2σ"), (m["high_threshold"], LINESTYLES[1], None)):
            ax.axvline(v, color=COLORS[1], linestyle=ls, linewidth=2, alpha=0.85, zorder=3, label=lab)
        ax.set_xlabel("QFLS (eV)")
        ax.set_ylabel("Fraction of pixels")
        ax.legend(framealpha=0.5, fontsize=10, loc="best", markerscale=1.5)
        _ticks(ax)
        _save(fig, out / "hist.png")
        made.append("hist.png")

        # 剖面
        fig = Figure(figsize=(7, 4))
        ax = fig.add_subplot()
        for i, (key, lab, n) in enumerate((("profile_x", "horizontal (center rows)", arr.shape[1]),
                                           ("profile_y", "vertical (center cols)", arr.shape[0]))):
            v = np.asarray(extra[key])
            x = np.linspace(0, n * (um or 1), len(v))
            ax.plot(x, v, color=COLORS[i], linestyle=LINESTYLES[i], linewidth=2, alpha=0.85, zorder=2, label=lab)
        ax.axhline(m["median"], color=COLORS[8], linestyle=LINESTYLES[3], linewidth=1.5, alpha=0.85)
        ax.set_xlabel("Position (µm)" if um else "Position (px)")
        ax.set_ylabel("QFLS (eV)")
        ax.legend(framealpha=0.5, fontsize=10, loc="best", markerscale=1.5)
        _ticks(ax)
        _save(fig, out / "profile.png")
        made.append("profile.png")

        # 异常图：灰度底图 + 低值区（蓝）/ 划痕（红）/ 亮点（橙）+ 边缘带、中心区的框
        fig = Figure(figsize=(6, 4))
        ax = fig.add_subplot()
        g = np.clip((small - lo) / (hi - lo or 1), 0, 1)
        rgb = np.repeat(g[..., None], 3, axis=2) * 0.85 + 0.1
        for name, col in (("low", COLORS[0]), ("scratch", COLORS[1]), ("bright", COLORS[2])):
            mk, _ = pool(extra["masks"][name], how="any")
            mk = mk[:rgb.shape[0], :rgb.shape[1]]
            rgb[mk] = matplotlib.colors.to_rgb(col)
        ax.imshow(rgb, extent=ext, interpolation="nearest")
        H, W = arr.shape
        s = um or 1
        b = extra["geometry"]["band"] * s
        r0, r1, c0, c1 = (v * s for v in extra["geometry"]["center"])
        ax.add_patch(matplotlib.patches.Rectangle((b, b), W * s - 2 * b, H * s - 2 * b, fill=False,
                                                  edgecolor=COLORS[8], linestyle="--", linewidth=1.5))
        ax.add_patch(matplotlib.patches.Rectangle((c0, r0), c1 - c0, r1 - r0, fill=False,
                                                  edgecolor=COLORS[3], linestyle=":", linewidth=1.5))
        handles = [matplotlib.patches.Patch(color=COLORS[0], label=f"low ({m['low_cc_count']})"),
                   matplotlib.patches.Patch(color=COLORS[1], label=f"scratch ({m['scratch_count']})"),
                   matplotlib.patches.Patch(color=COLORS[2], label=f"bright ({m['bright_count']})")]
        ax.legend(handles=handles, framealpha=0.5, fontsize=10, loc="upper right", markerscale=1.5)
        ax.set_xlabel(xl)
        ax.set_ylabel(yl)
        _ticks(ax)
        _save(fig, out / "anomaly.png")
        made.append("anomaly.png")

        # 变异函数
        vg = extra["variogram"]
        fig = Figure(figsize=(5, 4))
        ax = fig.add_subplot()
        x = np.asarray(vg["lags_px"], dtype=float) * (um or 1)
        y = np.asarray(vg["gamma"]) * 1e6
        ax.plot(x, y, color=COLORS[0], linestyle=LINESTYLES[0], linewidth=2, alpha=0.7, zorder=2)
        ax.scatter(x, y, facecolors="none", edgecolors=COLORS[0], marker="o", s=40, linewidths=2, zorder=3)
        ax.axhline(vg["sill"] * 1e6, color=COLORS[8], linestyle=LINESTYLES[1], linewidth=1.5)
        if m["variogram_range_px"] is not None:
            ax.axvline(m["variogram_range_px"] * (um or 1), color=COLORS[1], linestyle=LINESTYLES[2], linewidth=1.5)
        ax.set_xlabel("Lag (µm)" if um else "Lag (px)")
        ax.set_ylabel("γ (meV²)")
        _ticks(ax)
        _save(fig, out / "variogram.png")
        made.append("variogram.png")

    mosaic(out / "summary.png", [out / n for n in ("heatmap.png", "hist.png", "profile.png", "anomaly.png")],
           f"{title}   score {m['score']} ({m['grade']})")
    made.append("summary.png")
    return made


def mosaic(path: Path, parts: list[Path], header: str) -> None:
    """四张拼成 2 × 2，顶上一行字（只用 ASCII，免得没有中文字体）。"""
    from PIL import Image, ImageDraw, ImageFont
    ims = [Image.open(p).convert("RGB") for p in parts]
    cw = max(i.width for i in ims)
    ch = max(i.height for i in ims)
    top = 34
    canvas = Image.new("RGB", (cw * 2, ch * 2 + top), "white")
    for k, im in enumerate(ims):
        canvas.paste(im, ((k % 2) * cw + (cw - im.width) // 2, top + (k // 2) * ch + (ch - im.height) // 2))
    try:
        font = ImageFont.load_default(size=20)
    except TypeError:                     # Pillow < 10.1 没有 size
        font = ImageFont.load_default()
    ImageDraw.Draw(canvas).text((12, 6), header.encode("ascii", "replace").decode(), fill="black", font=font)
    canvas.save(path, compress_level=3)


def heatmap_fixed(path: Path, small: np.ndarray, lo: float, hi: float) -> None:
    """同一物镜一个色标的热图（多点对比用；没有坐标轴，色标在网页上画）。"""
    import matplotlib
    from PIL import Image
    lut = (matplotlib.colormaps[CMAP](np.linspace(0, 1, 256))[:, :3] * 255).astype(np.uint8)
    idx = np.clip((small - lo) / (hi - lo or 1) * 255, 0, 255).astype(np.uint8)
    Image.fromarray(lut[idx]).save(path, compress_level=3)


def colormap_stops(n: int = 9) -> list[str]:
    import matplotlib
    cm = matplotlib.colormaps[CMAP]
    return [matplotlib.colors.to_hex(cm(i / (n - 1))) for i in range(n)]


# ------------------------------------------------------------------ 全局
def global_figures(out: Path, rows: list[dict], codes: dict[str, str]) -> list[str]:
    """箱线图（按 实验 × 物镜 分组）、分数排名、等级分布。rows 只含有效样点。"""
    matplotlib, Figure, _ = _mpl()
    out.mkdir(parents=True, exist_ok=True)
    made = []
    if not rows:
        return made
    groups = sorted({(r["experiment"], r["objective"] or "?") for r in rows}, key=lambda g: (g[1], codes[g[0]]))
    lab = [f"{codes[e]}·{o}" for e, o in groups]
    keys = [("median", "Median QFLS (eV)"), ("p90_p10", "P90 − P10 (eV)"), ("low_2s_frac", "Low < med−2σ (frac)"),
            ("morans_i", "Moran's I"), ("variogram_range_px", "Variogram range (px)"), ("score", "Quality score")]
    with _RC_LOCK, matplotlib.rc_context(RC):
        fig = Figure(figsize=(14, 8))
        for i, (k, yl) in enumerate(keys):
            ax = fig.add_subplot(2, 3, i + 1)
            data = [[r[k] for r in rows if (r["experiment"], r["objective"] or "?") == g and r.get(k) is not None]
                    for g in groups]
            bp = ax.boxplot([d or [np.nan] for d in data], patch_artist=True, widths=0.6)
            for j, box in enumerate(bp["boxes"]):
                box.set(facecolor=COLORS[j % len(COLORS)], alpha=0.5, edgecolor="black", linewidth=1.5)
            for med in bp["medians"]:
                med.set(color="black", linewidth=2)
            ax.set_xticks(range(1, len(lab) + 1), lab, rotation=40, ha="right", fontsize=10)
            ax.set_ylabel(yl, fontsize=12)
            if k == "morans_i":
                ax.axhline(0.15, color=COLORS[1], linestyle=LINESTYLES[1], linewidth=1.5)
            _ticks(ax)
            ax.tick_params(axis="x", labelsize=10)
        _save(fig, out / "boxplots.png")
        made.append("boxplots.png")

        ranked = sorted(rows, key=lambda r: -r["score"])
        fig = Figure(figsize=(8, max(4, 0.22 * len(ranked) + 1)))
        ax = fig.add_subplot()
        gcol = {"A": COLORS[4], "B": COLORS[0], "C": COLORS[2], "D": COLORS[1]}
        y = np.arange(len(ranked))[::-1]
        ax.barh(y, [r["score"] for r in ranked], color=[gcol[r["grade"]] for r in ranked], alpha=0.8,
                edgecolor="black", linewidth=1.0, height=0.7)
        ax.set_yticks(y, [r["code"] for r in ranked], fontsize=8)
        ax.set_xlabel("Quality score")
        ax.set_xlim(0, 100)
        handles = [matplotlib.patches.Patch(color=c, label=g) for g, c in gcol.items()]
        ax.legend(handles=handles, framealpha=0.5, fontsize=10, loc="lower right", markerscale=1.5)
        _ticks(ax)
        ax.tick_params(axis="y", labelsize=8)
        _save(fig, out / "ranking.png")
        made.append("ranking.png")

        fig = Figure(figsize=(8, 4))
        ax = fig.add_subplot()
        bottom = np.zeros(len(groups))
        for g, c in gcol.items():
            v = np.array([sum(1 for r in rows if (r["experiment"], r["objective"] or "?") == gr and r["grade"] == g)
                          for gr in groups], dtype=float)
            ax.bar(range(len(groups)), v, bottom=bottom, color=c, alpha=0.8, edgecolor="black", linewidth=1.0,
                   width=0.6, label=g)
            bottom += v
        ax.set_xticks(range(len(groups)), lab, rotation=30, ha="right", fontsize=10)
        ax.set_ylabel("Points")
        ax.legend(framealpha=0.5, fontsize=10, loc="best", markerscale=1.5)
        _ticks(ax)
        ax.tick_params(axis="x", labelsize=10)
        _save(fig, out / "grades.png")
        made.append("grades.png")
    return made

