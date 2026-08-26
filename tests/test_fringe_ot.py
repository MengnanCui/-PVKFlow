"""干涉条纹法光学厚度 —— 对着冻结规范验。

这里测的不是「代码跑不跑得起来」，而是**规范里那些不能违反的条款**：
STEP 3 的参考数值、判据只打标志不改数值、块 A–D 一个都不能少。
"""
import numpy as np
import pytest

from app.analysis import fringe_ot as fo


def synth(ot_nm, lam=None, contrast=0.25, noise=0.002, seed=3):
    """一条已知 OT 的合成谱。f = 2·OT，这是全部物理。"""
    rng = np.random.default_rng(seed)
    lam = np.linspace(700.0, 1150.0, 1200) if lam is None else lam
    ot = np.atleast_1d(np.asarray(ot_nm, dtype=float))
    t = np.arange(ot.size, dtype=float)
    M = np.empty((lam.size, ot.size))
    for j, v in enumerate(ot):
        env = 0.8 * (1 - 0.05 * (lam - lam[0]) / (lam[-1] - lam[0]))
        M[:, j] = env * (1 + contrast * np.cos(2 * np.pi * 2 * v / lam))
    return lam, t, M + rng.normal(0, noise, M.shape)


# ---------------------------------------------------------------- STEP 3
def test_resolution_diagnostics_match_the_spec_reference_values():
    """规范 §4 STEP 3 白纸黑字给了 780–1050 nm 的参考值。

    对不上就说明 k 域的定义写错了（比如用了 2π/λ 而不是 1/λ）——
    这种错后面每一步都还能跑，只是答案系统性地差一个 2π。
    """
    d = fo.diagnostics_for(780, 1050)
    assert d["dk_range"] == pytest.approx(3.297e-4, rel=1e-3)
    assert d["bin_f_nm"] == pytest.approx(3033.3, rel=1e-4)
    assert d["ot_floor_nm"] == pytest.approx(2275.0, rel=1e-4)


def test_wider_window_lowers_the_floor():
    """窗口越宽 → bin 越小 → 能测的膜越薄。这是选 775–1120 的理由。"""
    narrow = fo.diagnostics_for(780, 1050)
    wide = fo.diagnostics_for(775, 1120)
    assert wide["ot_floor_nm"] < narrow["ot_floor_nm"]
    assert wide["bin_f_nm"] < narrow["bin_f_nm"]


# ---------------------------------------------------------------- f = 2nd
@pytest.mark.parametrize("ot_true", [7600.0, 6000.0, 4200.0])
def test_recovers_optical_thickness(ot_true):
    """条纹数 ≥ 3（标 OK）时，偏差要在一个量化格距以内。"""
    lam, t, M = synth(ot_true)
    res = fo.extract_series(lam, t, M, target_times_s="all",
                            window_nm=fo.PLATFORM_WINDOW_NM)
    q = res["points"][0]
    assert q["status"] == "OK"
    # 锁对条纹级次即可 —— 补零 argmax 的精度地板就是一格
    assert abs(q["ot_nm"] - ot_true) < res["diagnostics"]["ot_quantum_nm"]
    assert q["f_nm"] == pytest.approx(2 * q["ot_nm"])      # f = 2·OT，定义


def test_degraded_band_really_is_worse():
    """§4.1 的分级不是装饰：1.5–3 条纹那一档，偏差真的会到几个百分点。

    规范给的实测对照是 cycles 2.37 → -4.12%。这里 OT=3000 对应约 2.25 条纹，
    偏差应该明显大于 OK 档，但仍在「几个百分点」量级 —— 而不是锁错级次的几十个百分点。
    """
    lam, t, M = synth(3000.0)
    res = fo.extract_series(lam, t, M, target_times_s="all",
                            window_nm=fo.PLATFORM_WINDOW_NM)
    q = res["points"][0]
    assert q["status"] == "DEGRADED"
    rel = abs(q["ot_nm"] - 3000.0) / 3000.0
    assert res["diagnostics"]["ot_quantum_nm"] / 3000.0 < rel < 0.10


def test_absorbance_input_is_restored_before_fft():
    """吸光度 A = -log10(T) 必须先还原成 T（规范 §2 / STEP 0）。

    机理是 log 的非线性会在功率谱上生成 2f、3f 假峰。这里直接量那个比值：
    还原后基本为 0，不还原时随条纹对比度一路涨上去。

    实测下来，这几个对比度下假峰还没大到能把 argmax 从基频上抢走 ——
    所以 OT 数值是一样的。谐波是**风险**，不是必然翻车；
    但风险随对比度增长，湿膜那一段对比度最高，正是最危险的时候。
    """
    ot_true = 6000.0
    ratios = []
    for contrast in (0.25, 0.6, 0.92):
        lam, _, T = synth(ot_true, contrast=contrast, noise=0.0)
        A = -np.log10(np.clip(T[:, 0], 1e-6, None))
        kw = dict(window_nm=fo.PLATFORM_WINDOW_NM)
        good = fo.spectrum_at(lam, A, input_is_absorbance=True, **kw)
        bad = fo.spectrum_at(lam, A, input_is_absorbance=False, **kw)

        assert abs(good["ot_nm"] - ot_true) < 200
        assert fo.harmonic_ratio(good) < 1e-3       # 还原后几乎没有 2f
        ratios.append(fo.harmonic_ratio(bad))

    assert ratios[-1] > 0.05                        # 高对比度下假峰很明显
    assert ratios == sorted(ratios)                 # 对比度越高，污染越重


# ---------------------------------------------------------------- 判据
def test_thin_film_is_flagged_not_silently_wrong():
    """薄到条纹数 < 1.5 时，FFT 会锁到噪声峰上。

    规范 §10 的要求是**打标志，绝不修改数值** —— 让人看到
    「算出来是多少 + 为什么不该信」，而不是拿到一个被悄悄改过的数。
    """
    lam, t, M = synth(500.0)          # 远低于 775–1120 的下限 1888 nm
    res = fo.extract_series(lam, t, M, target_times_s="all",
                            window_nm=fo.PLATFORM_WINDOW_NM)
    q = res["points"][0]
    assert not q["ok"]
    assert q["status"] == "LOW_CYCLES"
    assert any("LOW_CYCLES" in f for f in q["flags"])
    assert np.isfinite(q["ot_nm"])            # 数值照给，不置 NaN、不清零


def test_flags_never_change_the_number():
    """同一条谱，判据参数怎么改，OT 的数值都不能变。"""
    lam, t, M = synth(2200.0)
    a = fo.extract_series(lam, t, M, target_times_s="all",
                          window_nm=fo.PLATFORM_WINDOW_NM)["points"][0]
    b = fo.extract_series(lam, t, M, target_times_s="all",
                          window_nm=fo.PLATFORM_WINDOW_NM,
                          min_cycles=0.1, accurate_cycles=0.1, min_snr_db=-99,
                          min_pts_per_fringe=0)["points"][0]
    assert a["ot_nm"] == b["ot_nm"]
    assert not a["ok"] and b["ok"]            # 只有标志变了


def test_degraded_band_is_its_own_status():
    """1.5–3 条纹之间是「有解但已进入精度衰减区」，跟无解要分开报。"""
    d = fo.diagnostics_for(*fo.PLATFORM_WINDOW_NM)
    ot = 2.2 * d["bin_f_nm"] / 2          # 约 2.2 条纹
    lam, t, M = synth(ot)
    q = fo.extract_series(lam, t, M, target_times_s="all",
                          window_nm=fo.PLATFORM_WINDOW_NM)["points"][0]
    assert q["status"] == "DEGRADED"
    assert not q["ok"]


# ---------------------------------------------------------------- 输入契约
def test_transposed_matrix_is_rejected():
    """§10 检查清单第一条：形状是 (n_lambda, n_time)，不是转置。"""
    lam, t, M = synth([6000.0, 5000.0, 4000.0])
    with pytest.raises(fo.FringeError, match="转置"):
        fo.extract_series(lam, t, M.T, target_times_s="all")


def test_window_outside_data_fails_with_a_readable_message():
    lam, t, M = synth(6000.0)
    with pytest.raises(fo.FringeError, match="个点"):
        fo.extract_series(lam, t, M, target_times_s="all", window_nm=[200, 260])


def test_non_monotonic_time_is_rejected():
    lam, _, M = synth([6000.0, 5000.0])
    with pytest.raises(fo.FringeError, match="单调递增"):
        fo.extract_series(lam, [1.0, 0.0], M, target_times_s="all")


# ---------------------------------------------------------------- 时刻选择
def test_nearest_frame_no_interpolation():
    """取最接近的实测帧，不插值 —— 插值会造出不存在的条纹。"""
    lam, _, M = synth([7000.0, 5000.0, 3000.0])
    t = np.array([0.0, 10.0, 20.0])
    res = fo.extract_series(lam, t, M, target_times_s=[0, 2.5, 9.9, 25],
                            window_nm=fo.PLATFORM_WINDOW_NM)
    actual = [q["t"] for q in res["points"]]
    assert actual == [0.0, 0.0, 10.0, 20.0]       # 全是实测帧，没有中间值


def test_time_tolerance_skips_loudly():
    lam, _, M = synth([7000.0, 5000.0])
    t = np.array([0.0, 10.0])
    res = fo.extract_series(lam, t, M, target_times_s=[0, 100],
                            time_tolerance_s=1.0, window_nm=fo.PLATFORM_WINDOW_NM)
    assert len(res["points"]) == 1
    assert res["skipped"] and res["skipped"][0]["t_req"] == 100.0


# ---------------------------------------------------------------- §5 输出
def test_report_has_all_four_blocks():
    """§5：每一次运行都必须输出四个块，缺一不可。禁止简化、禁止省略。"""
    lam, t, M = synth([7000.0, 5000.0, 3000.0])
    res = fo.extract_series(lam, t, M, target_times_s="all",
                            window_nm=fo.PLATFORM_WINDOW_NM)
    txt = fo.format_report(res)
    assert "参数（本次运行实际使用）" in txt        # 块 A
    assert "分辨率诊断" in txt                      # 块 B
    assert "光学厚度结果" in txt                    # 块 C
    assert "必读声明" in txt                        # 块 D


def test_block_a_echoes_the_actual_window_not_the_default():
    """平台传的是 775–1120，块 A 必须回显它，不能回显 DEFAULTS 里的 780–1050。

    这正是「不改冻结的默认值、靠 override 传参」能成立的前提 ——
    回显的是本次实际用的值。
    """
    lam, t, M = synth(6000.0)
    res = fo.extract_series(lam, t, M, target_times_s="all",
                            window_nm=fo.PLATFORM_WINDOW_NM)
    txt = fo.format_report(res)
    assert "[775, 1120]" in txt
    assert fo.DEFAULTS["window_nm"] == [780, 1050]      # 默认值原封不动


def test_block_d_is_verbatim_and_mentions_the_unknown_angle():
    lam, t, M = synth(6000.0)
    txt = fo.format_report(fo.extract_series(lam, t, M, target_times_s="all",
                                             window_nm=fo.PLATFORM_WINDOW_NM))
    assert "入射角未知" in txt
    assert "θ_i = 0°" in txt
    assert "未换算几何厚度 d" in txt


def test_non_ok_rows_expand_their_flags_under_the_table():
    """§5 块 C：状态为非 OK 时，必须在表下逐行展开完整的 flag 说明文字。"""
    lam, t, M = synth([7000.0, 500.0])
    txt = fo.format_report(fo.extract_series(lam, t, M, target_times_s="all",
                                             window_nm=fo.PLATFORM_WINDOW_NM))
    assert "LOW_CYCLES: 窗内条纹数" in txt
    assert "低于频率分辨率下限" in txt


def test_truncated_table_says_how_many_were_folded():
    """行数太多时可以抽样显示，但**必须说**抽了多少，不能静默截断。"""
    lam, t, M = synth(np.linspace(7000, 3000, 60))
    txt = fo.format_report(fo.extract_series(lam, t, M, target_times_s="all",
                                             window_nm=fo.PLATFORM_WINDOW_NM),
                           max_rows=10)
    assert "共 60 帧" in txt and "没有丢弃" in txt


# ---------------------------------------------------------------- 自检
def test_selftest_passes():
    assert fo._selftest() == 0
