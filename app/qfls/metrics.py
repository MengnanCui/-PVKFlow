"""一帧 QFLS 图的指标、综合质量分、ABCD 等级、异常标记。

**PARAMS 里每一个数都是平台自己定的（不是你给的），登记在 D-162 … D-175，方法说明页原样列出。**
影响分数 / 等级 / 异常判定的那些标了「建议你确认」。处理时可以整体覆盖（pipeline.run 的 params）。

没有 scipy：连通域用「行程 + 并查集」自己写（numpy 向量化，一帧 1216 × 1824 约 0.1 秒），
Moran's I 和变异函数也是直接按定义算的。
"""
from __future__ import annotations

import math

import numpy as np

PARAMS: dict = {
    # 数据校验
    "valid_range_ev": [0.3, 3.0],          # QFLS 合理范围；以外的像素算「超范围」
    "invalid_out_of_range_frac": 0.05,     # 超范围像素 > 5% 整帧无效
    # 占比
    "low_abs_ev": 1.10,                    # 低荧光绝对阈值（eV）—— 不同物镜之间不可比
    "sigma_k": 2.0,                        # 「中位数 ± 2σ」里的 2；σ = 整帧标准差
    # 边缘 / 中心
    "edge_band_frac": 0.10,                # 边缘带宽 = 短边的 10%
    "center_frac": 0.50,                   # 中心区 = 中间 50% × 50%
    # 连通域
    "connectivity": 8,
    "smooth_px": 3,                        # 找低值连通域前先 3 × 3 均值平滑，免得单像素噪声连成一片
    "low_cc_min_px": 5,                    # 小于 5 像素的低值斑不算
    "scratch_min_px": 30,                  # 划痕候选：面积 ≥ 30 像素、
    "scratch_min_elong": 5.0,              #   长短轴比 ≥ 5、
    "scratch_min_len_frac": 0.05,          #   长度 ≥ 短边的 5%、
    "scratch_max_width_px": 12.0,          #   宽度 ≤ 12 像素（宽的低谷不是划痕），质心不在边缘带里（那是边缘效应）
    "bright_k": 3.0,                       # 亮点：> 中位数 + 3σ 的连通域、
    "bright_min_px": 3,                    #   面积 3 … 200 像素
    "bright_max_px": 200,
    # 空间统计
    "moran_grid": [32, 48],                # Moran's I 在 32 × 48 的块均值网格上算（rook 邻接）
    "moran_threshold": 0.15,               # > 0.15 提示结构不均（你给的）
    "variogram_bin": 4,                    # 变异函数先 4 × 4 合并像素
    "variogram_max_lag_frac": 0.33,        # 最大滞后 = 短边的 1/3
    "variogram_sill_frac": 0.95,           # 变程 = γ 第一次达到 95% 基台（基台 = 方差）的距离
    # 异常标记
    "flag_large_low_frac": 0.01,           # 最大低值连通域 > 1% 画面
    "flag_edge_ev": 0.02,                  # |边缘 − 中心| > 20 meV
    "flag_group_robust_z": 3.5,            # 同一 slot、同一物镜的平行点里，中位数的稳健 z（按 MAD）|z| > 3.5 算离群
    "flag_group_min_n": 4,                 # 至少 4 个平行点才做这个判断
    # 综合质量分：100 减各项扣分（每项封顶）
    "score": {
        "spread":    {"max": 25, "from": 0.015, "to": 0.075},   # P90 − P10（eV）
        "low_frac":  {"max": 15, "from": 0.03, "to": 0.10},     # 低于中位数 − 2σ 的占比
        "low_area":  {"max": 15, "from": 0.0, "to": 0.01},      # 最大低值连通域占画面比例
        "scratch":   {"max": 20, "per": 10},                    # 每条划痕候选扣 10
        "edge":      {"max": 10, "from": 0.005, "to": 0.030},   # |边缘 − 中心|（eV）
        "moran":     {"max": 10, "from": 0.15, "to": 0.50},     # Moran's I
        "bright":    {"max": 5, "from": 0.0, "to": 100.0},      # 亮点密度（个 / 百万像素）
    },
    "grades": {"A": 85, "B": 70, "C": 55},                      # ≥85 A、≥70 B、≥55 C、其余 D
}

# 进分数 / 进全局比较的指标：键、中文名、单位、能不能跨物镜比
METRICS = [
    ("mean", "均值", "eV", False),
    ("median", "中位数", "eV", False),
    ("std", "标准差", "eV", True),
    ("p90_p10", "P90 − P10", "eV", True),
    ("p99_p1", "P99 − P1", "eV", True),
    ("edge_minus_center", "边缘 − 中心", "eV", True),
    ("low_abs_frac", "低荧光绝对占比", "", False),
    ("low_2s_frac", "低于中位数 − 2σ 占比", "", True),
    ("high_2s_frac", "高于中位数 + 2σ 占比", "", True),
    ("low_cc_count", "低值连通域数", "个", False),
    ("low_cc_max_frac", "最大低值连通域占比", "", False),
    ("scratch_count", "划痕候选数", "条", True),
    ("bright_density", "亮点密度", "个/百万像素", False),
    ("morans_i", "Moran's I", "", True),
    ("variogram_range_px", "变异函数变程", "像素", False),
    ("score", "综合质量分", "分", True),
]


# ------------------------------------------------------------------ 小工具
def box_smooth(a: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return a
    p = k // 2
    b = np.pad(a, p, mode="edge")
    c = np.cumsum(np.cumsum(b, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def block_mean(a: np.ndarray, gr: int, gc: int) -> np.ndarray:
    H, W = a.shape
    bh, bw = H // gr, W // gc
    return a[:gr * bh, :gc * bw].reshape(gr, bh, gc, bw).mean(axis=(1, 3))


# ------------------------------------------------------------------ 连通域
def components(mask: np.ndarray, connectivity: int = 8) -> dict:
    """行程编码 + 并查集。返回每个连通域的面积、质心、长度、长短轴比，和每段行程属于谁（画图用）。"""
    H, W = mask.shape
    m = np.zeros((H, W + 2), dtype=np.int8)
    m[:, 1:-1] = mask
    d = np.diff(m, axis=1)
    row, cs = np.nonzero(d == 1)
    _, ce = np.nonzero(d == -1)
    n = len(row)
    empty = {"n": 0, "area": np.zeros(0), "length": np.zeros(0), "width": np.zeros(0), "elong": np.zeros(0),
             "cy": np.zeros(0), "cx": np.zeros(0), "runs": (row, cs, ce), "run_comp": np.zeros(0, int)}
    if n == 0:
        return empty
    K = W + 2
    skey = row * K + cs
    ekey = row * K + ce
    adj = 0 if connectivity == 8 else 1
    lo = np.searchsorted(ekey, (row - 1) * K + cs + adj, side="left")
    hi = np.searchsorted(skey, (row - 1) * K + ce - adj, side="right")
    cnt = np.maximum(hi - lo, 0)
    tot = int(cnt.sum())
    lab = np.arange(n)
    if tot:
        a = np.repeat(np.arange(n), cnt)
        b = np.repeat(lo, cnt) + (np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt))
        while True:
            la, lb = lab[a], lab[b]
            if np.array_equal(la, lb):
                break
            mn = np.minimum(la, lb)
            np.minimum.at(lab, la, mn)
            np.minimum.at(lab, lb, mn)
            while True:
                nl = lab[lab]
                if np.array_equal(nl, lab):
                    break
                lab = nl
            lab = lab.copy()
    _, comp = np.unique(lab, return_inverse=True)
    nc = int(comp.max()) + 1
    ln = (ce - cs).astype(np.float64)
    y = row.astype(np.float64)

    def S(k):
        k = k.astype(np.float64)
        return k * (k + 1) * (2 * k + 1) / 6

    sx = ln * (cs + ce - 1) / 2
    sxx = S(ce - 1) - S(cs - 1)
    agg = lambda w: np.bincount(comp, weights=w, minlength=nc)  # noqa: E731
    A = agg(ln)
    mx, my = agg(sx) / A, agg(ln * y) / A
    cxx = agg(sxx) / A - mx ** 2 + 1 / 12
    cyy = agg(ln * y * y) / A - my ** 2 + 1 / 12
    cxy = agg(y * sx) / A - mx * my
    tr, det = (cxx + cyy) / 2, np.sqrt(((cxx - cyy) / 2) ** 2 + cxy ** 2)
    l1, l2 = tr + det, np.maximum(tr - det, 1 / 12)
    return {"n": nc, "area": A, "length": np.sqrt(12 * l1), "width": np.sqrt(12 * l2), "elong": np.sqrt(l1 / l2),
            "cy": my, "cx": mx, "runs": (row, cs, ce), "run_comp": comp}


def paint(shape: tuple[int, int], cc: dict, keep: np.ndarray) -> np.ndarray:
    """把选中的连通域画成布尔图（keep 是每个连通域要不要）。"""
    out = np.zeros(shape, dtype=bool)
    row, cs, ce = cc["runs"]
    if not len(row) or not keep.any():
        return out
    sel = keep[cc["run_comp"]]
    row, cs, ce = row[sel], cs[sel], ce[sel]
    ln = ce - cs
    tot = int(ln.sum())
    idx = np.repeat(row * shape[1] + cs, ln) + (np.arange(tot) - np.repeat(np.cumsum(ln) - ln, ln))
    out.flat[idx] = True
    return out


# ------------------------------------------------------------------ 空间统计
def morans_i(a: np.ndarray, grid=(32, 48)) -> float | None:
    """块均值网格上的 Moran's I（rook 邻接、权重 1）。≈ 0 随机；> 0 相近的块聚在一起（结构不均）。"""
    H, W = a.shape
    gr, gc = max(2, min(grid[0], H // 2)), max(2, min(grid[1], W // 2))
    z = block_mean(a, gr, gc)
    z = z - z.mean()
    den = float((z * z).sum())
    if den <= 0:
        return None
    num = 2 * (float((z[:, 1:] * z[:, :-1]).sum()) + float((z[1:, :] * z[:-1, :]).sum()))
    wsum = 2 * (gr * (gc - 1) + (gr - 1) * gc)
    return z.size / wsum * num / den


def variogram(a: np.ndarray, bin_px: int = 4, max_lag_frac: float = 0.33, sill_frac: float = 0.95) -> dict:
    """横竖两个方向平均的经验半变异函数 γ(h)；变程 = γ 第一次到 sill_frac × 方差的距离（线性插值）。"""
    H, W = a.shape
    b = max(1, min(bin_px, min(H, W) // 24))
    Z = block_mean(a, H // b, W // b) if b > 1 else a
    var = float(Z.var())
    L = max(2, int(min(Z.shape) * max_lag_frac))
    gam = []
    for h in range(1, L + 1):
        gx = 0.5 * float(np.mean((Z[:, h:] - Z[:, :-h]) ** 2))
        gy = 0.5 * float(np.mean((Z[h:, :] - Z[:-h, :]) ** 2))
        gam.append((gx + gy) / 2)
    lags = [h * b for h in range(1, L + 1)]
    out = {"bin_px": b, "lags_px": lags, "gamma": gam, "sill": var, "range_px": None, "reached": False}
    if var <= 0:
        return out
    target = sill_frac * var
    prev_h, prev_g = 0.0, 0.0
    for h, g in zip(range(1, L + 1), gam):
        if g >= target:
            t = (target - prev_g) / (g - prev_g) if g > prev_g else 1.0
            out["range_px"] = (prev_h + t * (h - prev_h)) * b
            out["reached"] = True
            break
        prev_h, prev_g = h, g
    return out


# ------------------------------------------------------------------ 一帧
def _down(v: np.ndarray, n: int = 400) -> list[float]:
    if len(v) <= n:
        return [round(float(x), 6) for x in v]
    k = math.ceil(len(v) / n)
    m = len(v) // k * k
    return [round(float(x), 6) for x in v[:m].reshape(-1, k).mean(axis=1)]


def compute(arr: np.ndarray, P: dict | None = None, pixel_size_um: float | None = None) -> tuple[dict, dict]:
    """返回 (指标, 画图要的东西)。arr 已经校验过、超范围的已填补。"""
    P = P or PARAMS
    H, W = arr.shape
    N = arr.size
    q = np.percentile(arr, [0.1, 1, 10, 50, 90, 99, 99.9])
    p01, p1, p10, med, p90, p99, p999 = (float(x) for x in q)
    mean, std = float(arr.mean()), float(arr.std())
    k = P["sigma_k"]
    lo_thr, hi_thr = med - k * std, med + k * std

    # 边缘 / 中心
    band = max(1, round(P["edge_band_frac"] * min(H, W)))
    edge = np.ones((H, W), dtype=bool)
    edge[band:H - band, band:W - band] = False
    c = P["center_frac"]
    r0, r1 = int(H * (1 - c) / 2), int(math.ceil(H * (1 + c) / 2))
    c0, c1 = int(W * (1 - c) / 2), int(math.ceil(W * (1 + c) / 2))
    edge_mean, center_mean = float(arr[edge].mean()), float(arr[r0:r1, c0:c1].mean())

    # 低值连通域 / 划痕（在平滑后的图上找）、亮点（原图）
    sm = box_smooth(arr, int(P["smooth_px"]))
    low = components(sm < lo_thr, P["connectivity"])
    keep_low = low["area"] >= P["low_cc_min_px"]
    scratch = (keep_low & (low["area"] >= P["scratch_min_px"]) & (low["elong"] >= P["scratch_min_elong"])
               & (low["length"] >= P["scratch_min_len_frac"] * min(H, W))
               & (low["width"] <= P["scratch_max_width_px"])
               & (low["cy"] >= band) & (low["cy"] < H - band) & (low["cx"] >= band) & (low["cx"] < W - band))
    hi_cc = components(arr > med + P["bright_k"] * std, P["connectivity"]) if std > 0 else None
    keep_bright = ((hi_cc["area"] >= P["bright_min_px"]) & (hi_cc["area"] <= P["bright_max_px"])
                   if hi_cc else np.zeros(0, bool))
    low_max = float(low["area"][keep_low].max()) if keep_low.any() else 0.0

    vg = variogram(arr, P["variogram_bin"], P["variogram_max_lag_frac"], P["variogram_sill_frac"])
    mi = morans_i(arr, tuple(P["moran_grid"])) if std > 0 else None

    # 剖面：中间 5% 行 / 列的平均
    hb, vb = max(1, round(H * 0.025)), max(1, round(W * 0.025))
    prof_x = arr[H // 2 - hb:H // 2 + hb + 1].mean(axis=0)
    prof_y = arr[:, W // 2 - vb:W // 2 + vb + 1].mean(axis=1)
    edges = np.linspace(p01, p999, 101) if p999 > p01 else np.linspace(p01 - 1e-3, p01 + 1e-3, 101)
    counts, _ = np.histogram(arr, bins=edges)

    um = pixel_size_um
    m = {
        "rows": H, "cols": W, "pixels": N,
        "mean": mean, "median": med, "std": std,
        "p1": p1, "p10": p10, "p90": p90, "p99": p99,
        "p90_p10": p90 - p10, "p99_p1": p99 - p1,
        "edge_mean": edge_mean, "center_mean": center_mean, "edge_minus_center": edge_mean - center_mean,
        "low_abs_frac": float((arr < P["low_abs_ev"]).mean()),
        "low_2s_frac": float((arr < lo_thr).mean()), "high_2s_frac": float((arr > hi_thr).mean()),
        "low_threshold": lo_thr, "high_threshold": hi_thr,
        "low_cc_count": int(keep_low.sum()), "low_cc_max_px": low_max, "low_cc_max_frac": low_max / N,
        "scratch_count": int(scratch.sum()),
        "scratches": [{"length_px": round(float(low["length"][i]), 1), "area_px": int(low["area"][i]),
                       "elong": round(float(low["elong"][i]), 1),
                       "width_px": round(float(low["width"][i]), 1),
                       "cy": round(float(low["cy"][i]), 1), "cx": round(float(low["cx"][i]), 1)}
                      for i in np.flatnonzero(scratch)[:20]],
        "bright_count": int(keep_bright.sum()),
        "bright_density": float(keep_bright.sum()) / (N / 1e6),
        "morans_i": mi,
        "variogram_range_px": vg["range_px"], "variogram_reached": vg["reached"],
        "variogram_range_um": (vg["range_px"] * um) if (um and vg["range_px"] is not None) else None,
        "pixel_size_um": um,
        "low_cc_max_um2": (low_max * um * um) if um else None,
    }
    extra = {
        "hist": {"edges": [round(float(x), 6) for x in edges], "counts": counts.tolist()},
        "profile_x": _down(prof_x), "profile_y": _down(prof_y),
        "variogram": {k2: vg[k2] for k2 in ("bin_px", "lags_px", "gamma", "sill")},
        "geometry": {"band": band, "center": [r0, r1, c0, c1]},
        "masks": {"low": paint((H, W), low, keep_low & ~scratch), "scratch": paint((H, W), low, scratch),
                  "bright": paint((H, W), hi_cc, keep_bright) if hi_cc else np.zeros((H, W), bool)},
    }
    m["score"], m["penalties"] = score(m, P)
    m["grade"] = grade(m["score"], P)
    m["flags"] = metric_flags(m, P)
    return m, extra


# ------------------------------------------------------------------ 分数 / 等级 / 异常
def _ramp(v: float, a: float, b: float) -> float:
    if v is None:
        return 0.0
    return float(min(1.0, max(0.0, (v - a) / (b - a)))) if b > a else float(v > a)


def score(m: dict, P: dict) -> tuple[float, list[dict]]:
    S = P["score"]
    items = [
        ("spread", "离散（P90 − P10）", _ramp(m["p90_p10"], S["spread"]["from"], S["spread"]["to"]) * S["spread"]["max"]),
        ("low_frac", "低值占比（< 中位数 − 2σ）",
         _ramp(m["low_2s_frac"], S["low_frac"]["from"], S["low_frac"]["to"]) * S["low_frac"]["max"]),
        ("low_area", "最大低值连通域",
         _ramp(m["low_cc_max_frac"], S["low_area"]["from"], S["low_area"]["to"]) * S["low_area"]["max"]),
        ("scratch", "划痕候选", min(S["scratch"]["max"], S["scratch"]["per"] * m["scratch_count"])),
        ("edge", "边缘 − 中心", _ramp(abs(m["edge_minus_center"]), S["edge"]["from"], S["edge"]["to"]) * S["edge"]["max"]),
        ("moran", "结构不均（Moran's I）",
         _ramp(m["morans_i"], S["moran"]["from"], S["moran"]["to"]) * S["moran"]["max"]),
        ("bright", "亮点密度", _ramp(m["bright_density"], S["bright"]["from"], S["bright"]["to"]) * S["bright"]["max"]),
    ]
    pens = [{"code": c, "label": lab, "points": round(p, 1), "max": S[c]["max"]} for c, lab, p in items]
    return round(max(0.0, 100 - sum(p["points"] for p in pens)), 1), pens


def grade(s: float | None, P: dict) -> str | None:
    if s is None:
        return None
    g = P["grades"]
    return "A" if s >= g["A"] else "B" if s >= g["B"] else "C" if s >= g["C"] else "D"


def flag(code: str, level: str, text: str) -> dict:
    return {"code": code, "level": level, "text": text}


def metric_flags(m: dict, P: dict) -> list[dict]:
    out = []
    if m["std"] <= 1e-9:
        out.append(flag("flat", "warn", "整帧数值几乎不变 —— 探测器 / 导出可能有问题"))
    if m["low_cc_max_frac"] > P["flag_large_low_frac"]:
        out.append(flag("large_low_area", "warn",
                        f"大面积低值区：最大一块占画面 {m['low_cc_max_frac']:.1%}"))
    if m["scratch_count"]:
        out.append(flag("scratch", "warn", f"{m['scratch_count']} 条划痕候选"))
    if m["morans_i"] is not None and m["morans_i"] > P["moran_threshold"]:
        out.append(flag("structure", "warn", f"结构不均：Moran's I = {m['morans_i']:.2f}（> {P['moran_threshold']}）"))
    if abs(m["edge_minus_center"]) > P["flag_edge_ev"]:
        out.append(flag("edge_effect", "warn",
                        f"边缘效应：边缘比中心{'低' if m['edge_minus_center'] < 0 else '高'} "
                        f"{abs(m['edge_minus_center']) * 1000:.0f} meV"))
    if not m["variogram_reached"]:
        out.append(flag("variogram_open", "info", "变异函数在最大滞后内没到基台（大尺度趋势），变程记为空"))
    return out
