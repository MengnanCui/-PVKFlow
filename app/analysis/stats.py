"""构效关系用的统计：相关系数（带显著性）、箱线图的分位数。

不引 scipy —— 装机体积一直盯着（requirements.txt 开头），为了两个 p 值多装 40 MB 不值。
p 值用 t 分布，t 分布的尾概率用正则化不完全 β 函数算（Numerical Recipes 的连分式）。
tests/test_relation.py 里的参考值是在另一个装了 scipy 的环境里算好、原样抄过来的，
逐个比到小数点后 10 位。

口径（和 scipy 一致，也是教科书写法）：
- 皮尔逊 r 的 p：t = r·√((n−2)/(1−r²))，自由度 n−2，双侧
- 皮尔逊 r 的 95% 置信区间：Fisher z 变换，标准误 1/√(n−3)
- 斯皮尔曼 ρ：先把两列各自排名（并列取平均名次），再算皮尔逊；p 用同一个 t 近似
  —— 样品少（n < 10）时这个近似偏粗，界面上要说
- 箱线图：分位数用线性插值（numpy 默认，也是 matplotlib 箱线图的做法）；
  须 = 落在 [Q1 − 1.5·IQR, Q3 + 1.5·IQR] 之内最远的那个数据点，之外的算离群
"""
from __future__ import annotations

import math

import numpy as np

_FPMIN = 1e-300
_Z975 = 1.959963984540054          # 标准正态 97.5% 分位数


# ---------------------------------------------------------------- t 分布
def _betacf(a: float, b: float, x: float) -> float:
    """不完全 β 函数的连分式（修正 Lentz 法）。"""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > _FPMIN else _FPMIN)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _FPMIN else _FPMIN)
        c = 1.0 + aa / c
        c = c if abs(c) > _FPMIN else _FPMIN
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _FPMIN else _FPMIN)
        c = 1.0 + aa / c
        c = c if abs(c) > _FPMIN else _FPMIN
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-15:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 β 函数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """自由度 df 的 t 分布，|T| ≥ |t| 的概率。"""
    if not math.isfinite(t):
        return 0.0
    return betainc(df / 2.0, 0.5, df / (df + t * t))


# ---------------------------------------------------------------- 相关
def rankdata(a) -> np.ndarray:
    """排名，从 1 起；并列的取平均名次（和 scipy.stats.rankdata 的默认一样）。"""
    a = np.asarray(a, dtype=float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    sorted_a = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def _r(x: np.ndarray, y: np.ndarray) -> float | None:
    dx, dy = x - x.mean(), y - y.mean()
    den = math.sqrt(float(dx @ dx) * float(dy @ dy))
    if den == 0.0:
        return None                      # 有一列全一样：相关系数没有定义
    return max(-1.0, min(1.0, float(dx @ dy) / den))


def _p_for_r(r: float, n: int) -> float:
    if abs(r) >= 1.0:
        return 0.0
    df = n - 2
    return t_two_sided_p(r * math.sqrt(df / (1.0 - r * r)), df)


def correlation(x, y) -> dict:
    """皮尔逊 + 斯皮尔曼。输入两列等长的数；NaN / inf 的那一对两边一起扔掉。

    返回里每个数都可能是 None —— 样品不够、或者有一列全一样时，**不给一个看起来正常的数**，
    而是在 `why` 里说为什么没有。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    n = int(len(x))
    out = {"n": n, "pearson": None, "spearman": None, "fit": None, "why": ""}
    if n < 3:
        out["why"] = f"只有 {n} 个样品同时有这两个数，至少要 3 个才算得了相关"
        return out
    if np.all(x == x[0]) or np.all(y == y[0]):
        out["why"] = "有一列所有样品都一样，相关系数没有定义"
        return out

    r = _r(x, y)
    pearson = {"r": r, "p": _p_for_r(r, n), "ci95": None}
    if n > 3 and abs(r) < 1.0:
        z, se = math.atanh(r), 1.0 / math.sqrt(n - 3)
        pearson["ci95"] = [math.tanh(z - _Z975 * se), math.tanh(z + _Z975 * se)]
    out["pearson"] = pearson

    rho = _r(rankdata(x), rankdata(y))
    if rho is not None:
        out["spearman"] = {"rho": rho, "p": _p_for_r(rho, n), "approx": n < 10}

    # 最小二乘直线：只是给散点图画一条参考线，不是模型
    slope, intercept = np.polyfit(x, y, 1)
    out["fit"] = {"slope": float(slope), "intercept": float(intercept)}
    return out


# ---------------------------------------------------------------- 箱线图
def box_stats(values) -> dict:
    """一组数的箱线图统计。返回离群点的下标（调用方拿去对回样品）。"""
    v = np.asarray(values, dtype=float)
    idx = np.flatnonzero(np.isfinite(v))
    v = v[idx]
    n = int(len(v))
    if n == 0:
        return {"n": 0}
    q1, med, q3 = (float(q) for q in np.percentile(v, [25, 50, 75]))
    iqr = q3 - q1
    lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = v[(v >= lo_fence) & (v <= hi_fence)]
    out_mask = (v < lo_fence) | (v > hi_fence)
    return {
        "n": n, "mean": float(v.mean()), "min": float(v.min()), "max": float(v.max()),
        "q1": q1, "median": med, "q3": q3,
        "whisker_lo": float(inside.min()), "whisker_hi": float(inside.max()),
        "outliers": [int(i) for i in idx[out_mask]],
    }
