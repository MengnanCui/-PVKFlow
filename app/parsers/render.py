"""把光谱矩阵变成能看的东西。

分工原则：
  * 位图（热力图、条纹图）—— 服务端渲染成 PNG。10⁶ 个 SVG 矩形会卡死浏览器，
    而同一张图 PNG 只要几百 KB。这不是"死图"，热力图本来就是位图。
  * 曲线（膜厚、斜率、积分）—— 只返回数值，前端画矢量图，可悬停、可框选、
    可导出、跟随明暗主题。
"""
from __future__ import annotations

import io
from typing import Literal

import numpy as np

Norm = Literal["none", "frame", "global", "wavelength"]
NORMS = ("none", "frame", "global", "wavelength")
# 热力图的纵轴。k = 1/λ 那一档给干涉条纹用 —— 条纹的相位对波数才是线性的。
AXES = ("wavelength", "wavenumber")

# 色标锚点。两条都刻意做成亮度单调递增 —— 否则热力图上会出现
# 数值不同但看起来一样亮的区域，人眼会读错。
COLORMAPS: dict[str, list[tuple[float, float, float]]] = {
    # 灰度：干涉条纹的传统画法，明暗即强弱，没有色相干扰
    "gray": [(0, 0, 0), (255, 255, 255)],
    # 深蓝 → 青 → 黄 → 白，亮度单调
    "ice": [(8, 12, 48), (18, 62, 122), (20, 122, 150),
            (64, 176, 140), (186, 208, 92), (248, 236, 152), (255, 255, 255)],
    # 单色蓝，取自平台主色，适合叠在界面里不抢眼
    "steel": [(9, 18, 30), (24, 62, 96), (36, 112, 160), (120, 176, 208), (240, 248, 252)],
    # 彩虹（蓝→青→绿→黄→红）。**亮度不单调**，中间的黄比两端都亮，
    # 所以读绝对数值时会有伪边界。但强度分布的整体形状看得最清楚，
    # 这也是光谱仪软件的惯用画法 —— 光谱处理那张图默认用它。
    "rainbow": [(48, 18, 130), (34, 74, 200), (28, 150, 208), (42, 190, 150),
                (128, 210, 74), (226, 206, 52), (238, 138, 40), (206, 46, 44)],
}


def _lut(name: str) -> np.ndarray:
    anchors = COLORMAPS.get(name, COLORMAPS["gray"])
    a = np.asarray(anchors, dtype=np.float64)
    xs = np.linspace(0, 1, len(a))
    out = np.empty((256, 3), dtype=np.uint8)
    g = np.linspace(0, 1, 256)
    for c in range(3):
        out[:, c] = np.clip(np.interp(g, xs, a[:, c]), 0, 255).astype(np.uint8)
    return out


def normalize(M: np.ndarray, mode: Norm = "frame") -> np.ndarray:
    """把矩阵压到 0–1。

    frame       每个时刻各自归一化 —— 看谱形随时间怎么变，不被总强度漂移干扰
    wavelength  每个波长各自归一化 —— 看某个波长的时间演化
    global      全局线性拉伸 —— 保留绝对强度关系
    none        只做裁剪
    """
    A = np.asarray(M, dtype=np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        if mode == "frame":
            lo = np.nanmin(A, axis=0, keepdims=True)
            hi = np.nanmax(A, axis=0, keepdims=True)
        elif mode == "wavelength":
            lo = np.nanmin(A, axis=1, keepdims=True)
            hi = np.nanmax(A, axis=1, keepdims=True)
        elif mode == "global":
            lo, hi = np.nanmin(A), np.nanmax(A)
        else:
            return np.clip(np.nan_to_num(A, nan=0.0), 0, 1)
        out = (A - lo) / np.where((hi - lo) == 0, 1, hi - lo)
    return np.clip(np.nan_to_num(out, nan=0.0), 0, 1)


def to_wavenumber(M: np.ndarray, lam: np.ndarray, n_k: int | None = None
                  ) -> tuple[np.ndarray, np.ndarray]:
    """换到等间隔波数网格 k = 1/λ。

    干涉相位对 k 线性，所以只有在 k 轴上条纹才是等周期的 —— 这正是
    "光栅式明暗条纹图"该有的样子。λ 等间隔取倒数后 k 就不等间隔了，
    直接画会把条纹拉歪。
    """
    lam = np.asarray(lam, dtype=np.float64)
    k_raw = 1.0 / lam                      # λ 升序 → k 降序
    order = np.argsort(k_raw)
    k_sorted = k_raw[order]
    A = np.asarray(M, dtype=np.float32)[order]

    n = int(n_k or len(lam))
    k = np.linspace(k_sorted[0], k_sorted[-1], n)

    # 向量化的线性插值：逐列调 np.interp 要几百毫秒，这样只要几毫秒。
    # np.interp 不接受 2D，所以自己算索引和权重，一次把整个矩阵插完。
    idx = np.clip(np.searchsorted(k_sorted, k, side="right") - 1, 0, len(k_sorted) - 2)
    span = k_sorted[idx + 1] - k_sorted[idx]
    w = np.where(span > 0, (k - k_sorted[idx]) / np.where(span == 0, 1, span), 0.0)
    w = w.astype(np.float32)[:, None]
    out = A[idx] * (1.0 - w) + A[idx + 1] * w
    return k, out.astype(np.float32)


def render_png(
    M: np.ndarray,
    *,
    cmap: str = "gray",
    norm: Norm = "frame",
    max_width: int = 1100,
    max_height: int = 560,
) -> tuple[bytes, dict]:
    """矩阵 (L, T) → PNG 字节流。

    图上是 **横轴时间、纵轴波长/波数，波长向上increases** —— in-situ 监测的
    习惯画法。max_width 约束时间方向，max_height 约束波长方向。

    自适应降采样：按矩阵实际尺寸决定缩放，不写死参数。
    用面积平均而不是抽点 —— 条纹是高频信号，直接抽点会产生摩尔纹假象。
    """
    from PIL import Image

    A = normalize(M, norm)                # (L, T) in 0..1，行=波长 列=时间
    img_arr = A[::-1]                     # 图像第 0 行在最上面 → 波长向上增大

    h_src, w_src = img_arr.shape          # h=波长, w=时间
    gray = Image.fromarray(
        np.clip(img_arr * 255.0 + 0.5, 0, 255).astype(np.uint8), mode="L")

    # 两个轴是不同物理量（时间 / 波长），不需要保持长宽比 —— 各自独立缩放，
    # 图才能填满面板。这也是所有 spectrogram 的画法。
    def fit(src: int, cap: int) -> int:
        if src > cap:
            return cap                       # 降采样：下面用 BOX 面积平均抗混叠
        if src * 4 <= cap:
            return src * (cap // src)        # 太稀疏时整数倍放大，避免浏览器插值糊掉
        return src                           # 其余交给 CSS 拉伸，反正是平滑数据

    tw, th = fit(w_src, max_width), fit(h_src, max_height)
    if (tw, th) != (w_src, h_src):
        # 缩小用 BOX（面积平均，抗条纹摩尔纹）；放大用 NEAREST（保持格子清晰）
        gray = gray.resize((tw, th),
                           Image.BOX if (tw < w_src or th < h_src) else Image.NEAREST)

    # 先缩放再上色：省掉对全尺寸 RGB 数组的处理，也因颜色数变少让 PNG 更小
    im = Image.fromarray(_lut(cmap)[np.asarray(gray)], mode="RGB")

    buf = io.BytesIO()
    # optimize=True 会多花 8 倍时间只换来 2% 体积，不值
    im.save(buf, "PNG", compress_level=3)
    data = buf.getvalue()
    return data, {
        "source_shape": [int(w_src), int(h_src)],       # [时间, 波长]
        "rendered_shape": [im.width, im.height],
        "downsampled": bool(tw < w_src or th < h_src),
        "bytes": len(data),
        "cmap": cmap,
        "norm": norm,
    }


# ------------------------------------------------------------------ 派生曲线
def band_integral(M: np.ndarray, lam: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """某波段的积分 vs 时间。用梯形法对 λ 积分，不是简单求和 ——
    这样换波段边界时结果连续，不会因为多算少算一个点而跳变。"""
    mask = (lam >= lo) & (lam <= hi)
    if mask.sum() < 2:
        return np.full(M.shape[1], np.nan)
    return np.trapezoid(M[mask], lam[mask], axis=0)


def wavelength_slope(M: np.ndarray, lam: np.ndarray, center: float,
                     half_width: float = 10.0) -> np.ndarray:
    """某波长处的谱斜率 dI/dλ vs 时间。在 ±half_width 窗口里做线性拟合，
    比两点差分抗噪。"""
    mask = (lam >= center - half_width) & (lam <= center + half_width)
    if mask.sum() < 3:
        return np.full(M.shape[1], np.nan)
    x = lam[mask]
    xc = x - x.mean()
    denom = float((xc ** 2).sum())
    if denom == 0:
        return np.full(M.shape[1], np.nan)
    return (xc[:, None] * M[mask]).sum(axis=0) / denom


def pick_frames(t: np.ndarray, n: int) -> np.ndarray:
    """在时间轴上等间隔挑 n 帧，首尾必取。"""
    T = len(t)
    if T <= n:
        return np.arange(T)
    return np.unique(np.linspace(0, T - 1, n).round().astype(int))
