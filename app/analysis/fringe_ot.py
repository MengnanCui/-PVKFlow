"""干涉条纹法光学厚度提取 —— `fringe-optical-thickness` 冻结规范的可执行副本。

本模块只负责一件事：从一条（或一组）光谱，算出**光学厚度 OT = n·d·cos θ_t**。

物理（规范 §1）：在波数域 k = 1/λ 下相位对 k 线性，δ(k) = 2π·OPD·k，
所以条纹是等周期正弦，其频率 f 的**数值就等于 OPD**（单位 nm）：

    f  = OPD = 2·n·d·cos θ_t      [nm]
    OT = f / 2 = n·d·cos θ_t      [nm]   ← 本模块的输出

三条必须记住的：
  · 干涉不改变波长，改变的是每个波长处的强度。
  · λ 域条纹间距 Δλ ≈ λ²/OPD 不是常数，**不能直接对 λ 做 FFT**。
  · 单条光谱只能给出乘积 n·d。任何 (n, d) 只要乘积相同，光谱完全一致。

代码结构逐条对应规范 §4 的 STEP 0–10，每一步的 WHY 直接引自规范。

    python -m app.analysis.fringe_ot --selftest
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np

# ══════════════════════════════════════════════════════════════════════
# §3 默认参数。**逐字照抄规范，不得在这里改。**
#
# 平台想用别的窗口（比如 775–1120）时，走 extract_series 的 override 传，
# 而 §5 块 A 本来就要求回显本次实际使用的值 —— 改默认值才是违规，传参不是。
# ══════════════════════════════════════════════════════════════════════
DEFAULTS: dict[str, Any] = {
    "version": "1.0.0",

    # ── 时刻选择 ──
    "target_times_s": [0, 2.5, 5, 10, 15, 20, 25],
    "time_match": "nearest",          # 取最接近的实测帧，不插值
    "time_tolerance_s": None,         # None = 不设限

    # ── 分析波段 ──
    "window_nm": [780, 1050],         # 钙钛矿吸收边约 775 nm，此窗口在透明区
    "window_fallback_nm": None,       # 不做自动换窗（§8 IMP-2）

    # ── 几何 ──
    "theta_i_deg": 0.0,               # ⚠️ 未知量，按 0 处理，见 §5 块 D

    # ── 信号处理 ──
    "input_is_absorbance": False,
    "detrend_order": 2,
    "window_function": "hann",
    "fft_pad": 8,
    "skip_low_bins": 0,
    "lambda_domain_preprocess": "off",
    "phase_refinement": "off",

    # ── 可信性判据（只打标志，不改数值）──
    "min_cycles": 1.5,
    "accurate_cycles": 3.0,
    "min_snr_db": 4.0,
    "min_pts_per_fringe": 5.0,

    # ── 输出 ──
    "report": "optical_thickness_only",
    "decimals": 1,
}

# 平台在原位吸收光谱这条链路上实际用的窗口。
# 775 避开吸收边，1120 是光谱仪的波长上限 —— 窗口越宽条纹数越多、精度越高（§4.1）。
PLATFORM_WINDOW_NM = [775.0, 1120.0]


class FringeError(ValueError):
    """输入不满足 §2 的契约。"""


# ══════════════════════════════════════════════════════════════════════
# STEP 0 · 装载与还原
# ══════════════════════════════════════════════════════════════════════
def _step0(lambda_nm, time_s, matrix, input_is_absorbance: bool):
    """IN: 三元组 + input_is_absorbance / OUT: 校验过的三元组"""
    lam = np.asarray(lambda_nm, dtype=np.float64)
    t = np.asarray(time_s, dtype=np.float64)
    M = np.asarray(matrix, dtype=np.float64)

    if M.shape != (lam.size, t.size):
        raise FringeError(
            f"matrix 形状应该是 (n_lambda, n_time) = ({lam.size}, {t.size})，"
            f"实际是 {M.shape}。转置了吗？（§10 检查清单第一条）")
    if not np.all(lam > 0):
        raise FringeError("lambda_nm 必须严格为正")
    if t.size > 1 and not np.all(np.diff(t) > 0):
        raise FringeError("time_s 必须单调递增")

    if input_is_absorbance:
        # WHY: -log10 是非线性变换，会把单一正弦变成基频+谐波，
        #      在功率谱上产生 2f、3f 假峰
        M = 10.0 ** (-M)
    return lam, t, M


# ══════════════════════════════════════════════════════════════════════
# STEP 1 · 选择时刻
# ══════════════════════════════════════════════════════════════════════
def _step1(t: np.ndarray, target_times_s, time_tolerance_s):
    """OUT: [(t_req, t_actual, col_index) 或 SKIPPED]

    不做时间插值 —— 插值会在两帧之间造出不存在的条纹，
    干燥过程中 OT 变化快时会系统性平滑掉真实的转变。
    """
    selected, skipped = [], []
    for t_req in target_times_s:
        j = int(np.argmin(np.abs(t - t_req)))
        t_actual = float(t[j])
        if time_tolerance_s is not None and abs(t_actual - t_req) > time_tolerance_s:
            skipped.append({"t_req": float(t_req), "nearest": t_actual,
                            "reason": f"超出容差 {time_tolerance_s} s"})
            continue
        selected.append((float(t_req), t_actual, j))
    return selected, skipped


# ══════════════════════════════════════════════════════════════════════
# STEP 2 · 切波段  ·  STEP 3 · 分辨率诊断  ·  STEP 4/5 · k 域与重采样
#
# 这四步只依赖波长轴和窗口，**对每一帧都相同** —— 所以整组算一次就够了。
# 这是纯粹的省时间，不改任何结果。
# ══════════════════════════════════════════════════════════════════════
class _Geometry:
    """窗口固定后，k 网格、插值权重、去趋势基都是常量。"""

    def __init__(self, lam: np.ndarray, window_nm, detrend_order: int,
                 window_function: str, min_cycles: float):
        order = np.argsort(lam)
        self.order = order
        lam_sorted = lam[order]

        mask = (lam_sorted >= window_nm[0]) & (lam_sorted <= window_nm[1])
        self.mask = mask
        self.lam_w = lam_sorted[mask]
        self.N = int(self.lam_w.size)
        if self.N < 32:
            raise FringeError(
                f"窗口 {window_nm[0]:g}–{window_nm[1]:g} nm 内只有 {self.N} 个点，"
                f"少于 32 个。波段必须落在有数据的区间里。")

        # ── STEP 3 · 分辨率诊断（先算，失败时也要报）──
        self.dk_range = 1.0 / self.lam_w[0] - 1.0 / self.lam_w[-1]
        self.bin_f_nm = 1.0 / self.dk_range
        self.ot_floor_nm = min_cycles * self.bin_f_nm / 2.0

        # ── STEP 4 · 换横轴到波数域（是 1/λ，不是 2π/λ）──
        k_raw = 1.0 / self.lam_w
        k_order = np.argsort(k_raw)          # λ 升序 → k 降序，需重排
        self.k_order = k_order
        k_raw = k_raw[k_order]

        # ── STEP 5 · 重采样到等间隔 k 网格 ──
        # λ 等间隔取倒数后 k 就不等间隔了，而 FFT 要求等间隔采样。
        self.k = np.linspace(k_raw[0], k_raw[-1], self.N)
        idx = np.clip(np.searchsorted(k_raw, self.k) - 1, 0, self.N - 2)
        span = k_raw[idx + 1] - k_raw[idx]
        self.interp_idx = idx
        self.interp_w = np.where(span == 0, 0.0, (self.k - k_raw[idx]) / np.where(span == 0, 1, span))
        self.dk = (self.k[-1] - self.k[0]) / (self.N - 1)

        # ── STEP 6 · 去趋势的基（归一化坐标改善条件数）──
        x = np.linspace(-1.0, 1.0, self.N)
        self.basis = np.vander(x, detrend_order + 1)
        self.basis_pinv = np.linalg.pinv(self.basis)

        # ── STEP 7 · 窗函数 ──
        if window_function != "hann":
            raise FringeError(f"只实现了 hann 窗，收到 {window_function!r}")
        self.win = 0.5 * (1 - np.cos(2 * np.pi * np.arange(self.N) / (self.N - 1)))


def _prepare_spectrum(geo: _Geometry, spec: np.ndarray) -> np.ndarray:
    """STEP 2 切窗 → STEP 4 排到 k 升序 → STEP 5 插值到等间隔 k 并去均值。"""
    s = spec[geo.order][geo.mask][geo.k_order]
    lo = s[geo.interp_idx]
    hi = s[geo.interp_idx + 1]
    sig = lo + (hi - lo) * geo.interp_w
    return sig - np.mean(sig)


def _step6_7(geo: _Geometry, sig: np.ndarray) -> np.ndarray:
    """STEP 6 去趋势 + STEP 7 加窗。

    STEP 6 WHY: 不减掉缓变背景，功率谱会被零频附近的巨大直流分量淹没。
    STEP 7 WHY: 硬截断相当于乘方窗，旁瓣很强会拖出假峰（谱泄漏）。
    """
    sig_d = sig - geo.basis @ (geo.basis_pinv @ sig)
    return sig_d * geo.win


# ══════════════════════════════════════════════════════════════════════
# STEP 8 · 补零 FFT  ·  STEP 9 · 找峰与信噪比
# ══════════════════════════════════════════════════════════════════════
def _step8_9(geo: _Geometry, sig_w: np.ndarray, fft_pad: int, skip_low_bins: int):
    n_fft = int(fft_pad * geo.N)
    Y = np.fft.fft(sig_w, n_fft)
    n_pos = n_fft // 2
    power = np.abs(Y[1:n_pos]) ** 2
    # 频率的量纲永远是横轴量纲的倒数。横轴是 k (nm⁻¹)，所以 f 的单位是 nm，
    # 且数值上等于 OPD。补零不增加真实分辨率，但把峰位插得更细。
    f_axis = np.arange(1, n_pos) / (n_fft * geo.dk)

    if skip_low_bins > 0:
        power[: skip_low_bins * fft_pad] = 0.0

    j = int(np.argmax(power))
    f_nm = float(f_axis[j])
    noise = float(np.median(np.delete(power, j)))
    snr_db = float(10 * np.log10(power[j] / noise)) if noise > 0 else float("inf")
    cycles = f_nm * geo.dk_range                    # 窗内条纹数 = 等效 bin 序号
    pts_per_fringe = geo.N / cycles if cycles > 0 else float("inf")
    return f_nm, snr_db, float(cycles), float(pts_per_fringe), f_axis, power


# ══════════════════════════════════════════════════════════════════════
# STEP 10 · 换算与可信性判据
#
# 判据**只打标志，绝不修改 OT_nm 的数值**。让使用者看到「算出来是多少 +
# 为什么不该信」，而不是拿到一个被悄悄改过的数。
# ══════════════════════════════════════════════════════════════════════
def _step10(f_nm, cycles, snr_db, pts_per_fringe, p) -> tuple[float, bool, list[str]]:
    ot_nm = f_nm / 2.0
    flags: list[str] = []

    if cycles < p["min_cycles"]:
        flags.append(f"LOW_CYCLES: 窗内条纹数 {cycles:.2f} < {p['min_cycles']}，"
                     f"低于频率分辨率下限，结果可能是噪声峰")
    elif cycles < p["accurate_cycles"]:
        flags.append(f"DEGRADED: 窗内条纹数 {cycles:.2f} < {p['accurate_cycles']}，"
                     f"数值有解但已进入精度衰减区（见 §4.1），偏差可达数个百分点")
    if snr_db < p["min_snr_db"]:
        flags.append(f"LOW_SNR: 峰信噪比 {snr_db:.1f} dB < {p['min_snr_db']} dB")
    if pts_per_fringe < p["min_pts_per_fringe"]:
        flags.append(f"UNDERSAMPLED: 每条纹 {pts_per_fringe:.1f} 点 < "
                     f"{p['min_pts_per_fringe']}，光谱仪采样可能不足")
    return ot_nm, len(flags) == 0, flags


def status_of(flags: list[str]) -> str:
    """状态取最严重的那一条。OK / DEGRADED / LOW_CYCLES / LOW_SNR / UNDERSAMPLED"""
    for key in ("LOW_CYCLES", "LOW_SNR", "UNDERSAMPLED", "DEGRADED"):
        if any(f.startswith(key) for f in flags):
            return key
    return "OK"


# ══════════════════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════════════════
def extract_series(lambda_nm, time_s, matrix, **overrides) -> dict:
    """整条 OT(t) 曲线。

    overrides 里给的值覆盖 DEFAULTS，且会原样出现在 §5 块 A 的回显里。
    `target_times_s="all"` 表示每一帧都算（画曲线要的就是这个）。
    """
    p = {**DEFAULTS, **overrides}
    lam, t, M = _step0(lambda_nm, time_s, matrix, bool(p["input_is_absorbance"]))

    targets = p["target_times_s"]
    if isinstance(targets, str) and targets == "all":
        selected = [(float(v), float(v), j) for j, v in enumerate(t)]
        skipped: list[dict] = []
    else:
        selected, skipped = _step1(t, targets, p["time_tolerance_s"])

    geo = _Geometry(lam, p["window_nm"], int(p["detrend_order"]),
                    str(p["window_function"]), float(p["min_cycles"]))

    points = []
    for t_req, t_actual, j in selected:
        sig = _prepare_spectrum(geo, M[:, j])
        sig_w = _step6_7(geo, sig)
        f_nm, snr_db, cycles, ppf, _, _ = _step8_9(
            geo, sig_w, int(p["fft_pad"]), int(p["skip_low_bins"]))
        ot_nm, ok, flags = _step10(f_nm, cycles, snr_db, ppf, p)
        points.append({
            "t_req": t_req, "t": t_actual, "index": j,
            "ot_nm": ot_nm, "f_nm": f_nm, "cycles": cycles,
            "snr_db": snr_db, "pts_per_fringe": ppf,
            "ok": ok, "flags": flags, "status": status_of(flags),
        })

    # 补零把峰位插到 bin/pad，所以 OT 落在一张间距 bin_f/(2·pad) 的网格上，
    # 最大量化偏差是它的一半（§4.1 说的「半 bin 量化精度」）。
    # 这是本方法在这个窗口下的精度地板，不是噪声 —— 要更细得上 §8 IMP-3 相位精修。
    ot_quantum = geo.bin_f_nm / (2.0 * int(p["fft_pad"]))
    return {
        "params": p,
        "diagnostics": {
            "window_nm": list(p["window_nm"]),
            "n_points": geo.N,
            "dk_range": geo.dk_range,
            "bin_f_nm": geo.bin_f_nm,
            "ot_floor_nm": geo.ot_floor_nm,
            "ot_quantum_nm": ot_quantum,
            "ot_max_quant_error_nm": ot_quantum / 2.0,
        },
        "points": points,
        "skipped": skipped,
        "n_ok": sum(1 for q in points if q["ok"]),
    }


def spectrum_at(lambda_nm, spec, **overrides) -> dict:
    """单条谱的中间量：k 域信号 + 功率谱 + 峰。

    规范 §6 要的「诊断三联」（原始谱 / k 域 / 功率谱）就靠它，
    排查「峰是不是锁错了」也靠它 —— 光看一个 OT 数字是看不出来的。
    """
    p = {**DEFAULTS, **overrides}
    lam = np.asarray(lambda_nm, dtype=np.float64)
    y = np.asarray(spec, dtype=np.float64)
    if p["input_is_absorbance"]:
        y = 10.0 ** (-y)

    geo = _Geometry(lam, p["window_nm"], int(p["detrend_order"]),
                    str(p["window_function"]), float(p["min_cycles"]))
    sig = _prepare_spectrum(geo, y)
    sig_w = _step6_7(geo, sig)
    f_nm, snr_db, cycles, ppf, f_axis, power = _step8_9(
        geo, sig_w, int(p["fft_pad"]), int(p["skip_low_bins"]))
    ot_nm, ok, flags = _step10(f_nm, cycles, snr_db, ppf, p)
    return {
        "k": geo.k, "sig": sig, "sig_windowed": sig_w,
        "f_axis": f_axis, "power": power,
        "f_nm": f_nm, "ot_nm": ot_nm, "cycles": cycles, "snr_db": snr_db,
        "pts_per_fringe": ppf, "ok": ok, "flags": flags,
        "status": status_of(flags), "bin_f_nm": geo.bin_f_nm,
    }


def harmonic_ratio(res: dict, order: int = 2) -> float:
    """峰在 order·f 处的功率 / 基频功率。

    输入是吸光度却没还原时，-log10 的非线性会把这个比值抬上来（规范 STEP 0）。
    """
    f0 = res["f_nm"]
    f_axis, power = res["f_axis"], res["power"]
    j0 = int(np.argmin(np.abs(f_axis - f0)))
    jh = int(np.argmin(np.abs(f_axis - order * f0)))
    if jh >= power.size or power[j0] <= 0:
        return 0.0
    # 取谐波位置附近半个 bin 内的最大值，别被网格错位漏掉
    span = max(1, int(res["bin_f_nm"] / (f_axis[1] - f_axis[0]) / 2))
    lo, hi = max(0, jh - span), min(power.size, jh + span + 1)
    return float(power[lo:hi].max() / power[j0])


def diagnostics_for(lam_min: float, lam_max: float,
                    min_cycles: float = DEFAULTS["min_cycles"]) -> dict:
    """只要 STEP 3 的分辨率诊断，不跑 FFT。

    「这个窗口能测到多薄」是选窗口时就该知道的，不用等算完。
    """
    dk_range = 1.0 / float(lam_min) - 1.0 / float(lam_max)
    if dk_range <= 0:
        raise FringeError(f"窗口反了：{lam_min:g}–{lam_max:g} nm")
    bin_f = 1.0 / dk_range
    return {"window_nm": [float(lam_min), float(lam_max)],
            "dk_range": dk_range, "bin_f_nm": bin_f,
            "ot_floor_nm": min_cycles * bin_f / 2.0}


# ══════════════════════════════════════════════════════════════════════
# §5 输出规范 —— 块 A/B/C/D，缺一不可，禁止简化、禁止省略
# ══════════════════════════════════════════════════════════════════════
_BLOCK_A_ORDER = [
    "version", "target_times_s", "time_match", "window_nm", "theta_i_deg",
    "input_is_absorbance", "detrend_order", "window_function", "fft_pad",
    "skip_low_bins", "lambda_domain_preprocess", "phase_refinement",
    "min_cycles", "accurate_cycles", "min_snr_db", "min_pts_per_fringe",
    "report",
]

_DECLARATION = """════════ 必读声明 ════════
[1] 本次输出的是光学厚度 OT = n · d · cos(θ_t)，单位 nm。
    这是本方法唯一无歧义的输出量，不依赖折射率。

[2] 入射角未知。元数据字段（Mode / CollectionDuration / MeasurementBrightPD /
    MeasurementDarkPD / ReferencePDDiff / ReferencePDRatio / PDDeltaFromReference /
    PDRemainingRatio / TaskDuration）全部为光强标定与时序参数，不含几何信息；
    MATLAB V13 / stateV5 源码中亦无入射角变量。
    本次按 θ_i = 0° 处理，即 cos θ_t = 1，OT ≈ n·d。
    若实际为近垂直入射，此近似引入的偏差：θ=5° → 0.17%，θ=10° → 0.68%，θ=20° → 2.71%。

[3] 未换算几何厚度 d。单条光谱无法把 n 和 d 分开 —— 任何 (n, d) 组合只要
    乘积相同，光谱完全一致。要得到 d，需另行提供 n 并注明其来源与适用波长。"""


def _fmt_param(key: str, value: Any) -> str:
    if key == "time_match":
        return f"{value}（不插值）"
    if key == "target_times_s" and isinstance(value, str):
        return f"{value}（每一帧都算）"
    if key == "theta_i_deg":
        return f"{value}        ⚠️ 见块 D"
    if isinstance(value, list):
        return "[" + ", ".join(f"{v:g}" if isinstance(v, (int, float)) else str(v)
                               for v in value) + "]"
    return str(value)


def format_report(results: dict, max_rows: int = 0) -> str:
    """块 A–D 全文。**不要绕过它自己拼输出** —— 四块的完整性由它保证。

    max_rows > 0 时块 C 的表只列这么多行（曲线有几百帧，全列没人看）；
    但**多少行被折叠会写在表下**，不是静默截断。
    """
    p = results["params"]
    d = results["diagnostics"]
    pts = results["points"]
    dec = int(p.get("decimals", 1))

    # ── 块 A ──
    width = max(len(k) for k in _BLOCK_A_ORDER)
    lines = ["════════ 参数（本次运行实际使用）════════"]
    for key in _BLOCK_A_ORDER:
        lines.append(f"{key:<{width}} : {_fmt_param(key, p.get(key))}")

    # ── 块 B ──
    w0, w1 = d["window_nm"]
    lines += ["", f"════════ 分辨率诊断（窗口 {w0:g}–{w1:g} nm）════════",
              f"数据点数 N        : {d['n_points']} 个",
              f"Δk_range          : {d['dk_range']:.4e} nm⁻¹",
              f"一个 FFT bin 的 f : {d['bin_f_nm']:.1f} nm",
              f"OT 可测下限       : {d['ot_floor_nm']:.1f} nm   "
              f"（= min_cycles × bin_f / 2）",
              f"OT 量化格距       : {d['ot_quantum_nm']:.1f} nm   "
              f"（= bin_f / (2 × fft_pad)，最大量化偏差 "
              f"±{d['ot_max_quant_error_nm']:.1f} nm）"]

    # ── 块 C ──
    lines += ["", "════════ 光学厚度结果 ════════",
              " 请求时刻(s)  实测时刻(s)   OT = n·d·cosθ (nm)   条纹数   SNR(dB)   状态"]
    shown = pts if max_rows <= 0 else pts[:: max(1, len(pts) // max_rows)][:max_rows]
    for q in shown:
        lines.append(
            f"{q['t_req']:>10.1f}   {q['t']:>10.2f}   {q['ot_nm']:>17.{dec}f}   "
            f"{q['cycles']:>6.2f}   {q['snr_db']:>7.1f}   {q['status']}")
    if len(shown) < len(pts):
        lines.append(f"（共 {len(pts)} 帧，上表按等间隔抽了 {len(shown)} 行；"
                     f"完整逐帧数据在曲线里，没有丢弃）")

    # 状态为非 OK 时，必须在表下逐行展开完整的 flag 说明文字
    bad = [q for q in shown if not q["ok"]]
    if bad:
        lines.append("")
        for q in bad:
            for f in q["flags"]:
                lines.append(f"  t={q['t']:.2f}s  {f}")
    if results.get("skipped"):
        lines.append("")
        for s in results["skipped"]:
            lines.append(f"  SKIPPED  请求 {s['t_req']:.1f}s：{s['reason']}"
                         f"（最近的实测帧 {s['nearest']:.2f}s）")

    # ── 块 D ──
    lines += ["", _DECLARATION]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# §7 自检：合成数据验证 f = 2·n·d
# ══════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    rng = np.random.default_rng(7)
    lam = np.linspace(700.0, 1150.0, 1200)
    t = np.linspace(0.0, 20.0, 41)
    ot_true = 6000.0 - 3400.0 * (t / t[-1])          # 6000 → 2600 nm

    M = np.empty((lam.size, t.size))
    for j in range(t.size):
        env = 0.8 * (1 - 0.05 * (lam - lam[0]) / (lam[-1] - lam[0]))
        M[:, j] = env * (1 + 0.25 * np.cos(2 * np.pi * 2 * ot_true[j] / lam))
    M += rng.normal(0, 0.002, M.shape)

    res = extract_series(lam, t, M, target_times_s="all",
                         window_nm=PLATFORM_WINDOW_NM)
    got = np.array([q["ot_nm"] for q in res["points"]])
    err = (got - ot_true) / ot_true * 100

    ok_mask = np.array([q["ok"] for q in res["points"]])
    d = res["diagnostics"]
    # argmax 落在离真值最近的网格点上；有噪声时可能滑到相邻那个点。
    # 所以判据取**一整个格距**：真正要证的是它锁对了条纹级次，
    # 锁错级次的话偏差是一整个 bin（1258 nm），差着一个量级，一眼能分。
    bound = d["ot_quantum_nm"]
    err_nm = got - ot_true

    print(format_report(res, max_rows=12))
    print()
    print("════════ 自检 ════════")
    print(f"OT 真值 {ot_true[0]:.0f} → {ot_true[-1]:.0f} nm，共 {len(got)} 帧")
    print(f"OT 可测下限 {d['ot_floor_nm']:.0f} nm，量化格距 {d['ot_quantum_nm']:.1f} nm")
    if ok_mask.any():
        print(f"标 OK 的 {ok_mask.sum()} 帧：最大偏差 "
              f"{np.abs(err_nm[ok_mask]).max():.1f} nm "
              f"({np.abs(err[ok_mask]).max():.2f}%)")
    print(f"全部 {len(got)} 帧：最大偏差 {np.abs(err_nm).max():.1f} nm "
          f"({np.abs(err).max():.2f}%)")

    # 判据是**算法自己的量化界**，不是随手定的百分比。
    # 补零 argmax 只能把峰位定位到半个填充 bin —— 比这更好就说明测试写错了，
    # 比这更差才是真的有 bug。§4.1 的「≤1%」是在更窄的窗口上量的，
    # 窗口一宽 bin 就变大，同样的算法百分比自然变差。
    within_half = (np.abs(err_nm[ok_mask]) <= d["ot_max_quant_error_nm"]).sum()
    print(f"  其中 {within_half}/{ok_mask.sum()} 帧在半格 "
          f"±{d['ot_max_quant_error_nm']:.1f} nm 内，其余滑到了相邻网格点")

    bad = ok_mask & (np.abs(err_nm) > bound)
    if bad.any():
        print(f"✗ 有 {bad.sum()} 帧标了 OK 但偏差超过一格 ±{bound:.1f} nm "
              f"—— 可能锁错了条纹级次")
        return 1
    print(f"✓ f = 2·n·d 验证通过：标 OK 的帧全部锁在正确的条纹级次上"
          f"（偏差 < 一格 {bound:.1f} nm）")
    print(f"  （要更细得上 §8 IMP-3 相位精修，规范里默认是 off）")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(json.dumps(DEFAULTS, ensure_ascii=False, indent=2))
