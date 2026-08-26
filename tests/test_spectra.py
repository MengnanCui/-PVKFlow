"""光谱矩阵：解析判向、缓存、波数重采样、渲染、派生曲线。"""
import numpy as np
import pytest

from app.parsers import matrix, render


def _synth(n_lam=200, n_t=60, ot=4000.0):
    """一段有已知光学厚度的干涉谱。"""
    lam = np.linspace(600, 1100, n_lam)
    t = np.linspace(0, 20, n_t)
    M = np.empty((n_lam, n_t), np.float32)
    for j in range(n_t):
        o = ot * (1 - 0.7 * j / max(1, n_t - 1))
        M[:, j] = 0.6 + 0.2 * np.cos(2 * np.pi * 2 * o / lam)
    return lam, t, M


def _write_wide(path, lam, t, M, preamble=()):
    with open(path, "w", encoding="utf-8") as f:
        for line in preamble:
            f.write(f"# {line}\n")
        f.write("Wavelength(nm)," + ",".join(f"{x:.4f}" for x in t) + "\n")
        for i, w in enumerate(lam):
            f.write(f"{w:.4f}," + ",".join(f"{v:.6f}" for v in M[i]) + "\n")


# ---------------------------------------------------------------- 解析
def test_parses_wide_format(tmp_path):
    lam, t, M = _synth()
    p = tmp_path / "wide.csv"
    _write_wide(p, lam, t, M)
    sm = matrix.parse(p)
    assert sm.orientation == "wavelength_rows"
    assert sm.shape == (len(lam), len(t))
    assert np.allclose(sm.lam, lam, atol=1e-3)
    assert np.abs(sm.M - M).max() < 1e-4


def test_parses_transposed_format(tmp_path):
    """行是时间、列是波长 —— 有些仪器就这么导。"""
    lam, t, M = _synth()
    p = tmp_path / "tall.csv"
    with open(p, "w", encoding="utf-8") as f:
        f.write("Time(s)," + ",".join(f"{x:.4f}" for x in lam) + "\n")
        for j, tv in enumerate(t):
            f.write(f"{tv:.4f}," + ",".join(f"{v:.6f}" for v in M[:, j]) + "\n")
    sm = matrix.parse(p)
    assert sm.orientation == "time_rows"
    assert sm.shape == (len(lam), len(t))       # 内部统一成 (波长, 时间)
    assert np.abs(sm.M - M).max() < 1e-4


def test_instrument_preamble_does_not_break_delimiter_detection(tmp_path):
    """注释行里的逗号数量和数据行完全不同，会把分隔符判断带偏。"""
    lam, t, M = _synth()
    p = tmp_path / "with_head.csv"
    _write_wide(p, lam, t, M, preamble=[
        "Instrument: InSituSpec-2", "Mode: Transmission",
        "MeasurementBrightPD: 48213", "CollectionDuration: 20.0",
    ])
    sm = matrix.parse(p)
    assert sm.shape == (len(lam), len(t))
    assert sm.meta["Instrument"] == "InSituSpec-2"
    assert sm.meta["MeasurementBrightPD"] == 48213.0


def test_descending_wavelength_is_normalised(tmp_path):
    lam, t, M = _synth()
    p = tmp_path / "desc.csv"
    _write_wide(p, lam[::-1], t, M[::-1])
    sm = matrix.parse(p)
    assert sm.lam[0] < sm.lam[-1]               # 统一成升序
    assert np.abs(sm.M - M).max() < 1e-4


def test_refuses_a_plain_two_column_file(tmp_path):
    p = tmp_path / "two.csv"
    p.write_text("Voltage,Current\n0,1\n0.1,2\n0.2,3\n0.3,4\n0.4,5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        matrix.parse(p)


def test_cache_round_trips_and_is_content_addressed(workspace, tmp_path):
    lam, t, M = _synth()
    p = tmp_path / "c.csv"
    _write_wide(p, lam, t, M)

    first = matrix.load_cached(p)
    cached = matrix._cache_path(
        __import__("hashlib").sha256(p.read_bytes()).hexdigest())
    assert cached.is_file()

    second = matrix.load_cached(p)
    assert np.array_equal(first.M, second.M)
    assert first.meta == second.meta

    # 文件内容变了 → sha 变了 → 旧缓存自然不会被命中，不需要写失效逻辑
    _write_wide(p, lam, t, M * 0.5)
    third = matrix.load_cached(p)
    assert np.abs(third.M - M * 0.5).max() < 1e-4


# ---------------------------------------------------------------- 波数
def test_wavenumber_resampling_matches_pointwise_interp():
    """向量化实现必须和逐列 np.interp 等价。"""
    lam, t, M = _synth(n_lam=300, n_t=20)
    k, out = render.to_wavenumber(M, lam)

    k_raw = 1.0 / lam
    order = np.argsort(k_raw)
    ks, A = k_raw[order], M[order]
    for j in range(0, M.shape[1], 5):
        ref = np.interp(k, ks, A[:, j])
        assert np.abs(out[:, j] - ref).max() < 1e-5


def test_wavenumber_grid_is_uniform():
    """条纹在 k 轴上等周期，前提是 k 网格本身等间隔。"""
    lam, _, M = _synth()
    k, _ = render.to_wavenumber(M, lam)
    d = np.diff(k)
    assert np.allclose(d, d[0], rtol=1e-9)


# ---------------------------------------------------------------- 渲染
def test_render_png_orientation_and_adaptive_downsample():
    lam, t, M = _synth(n_lam=2000, n_t=1500)
    png, info = render.render_png(M, max_width=600, max_height=400)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert info["source_shape"] == [1500, 2000]     # [时间, 波长]
    assert info["rendered_shape"] == [600, 400]
    assert info["downsampled"] is True


def test_small_matrix_is_upscaled_not_left_tiny():
    lam, t, M = _synth(n_lam=20, n_t=15)
    _, info = render.render_png(M, max_width=600, max_height=400)
    assert info["rendered_shape"][0] > 20
    assert info["downsampled"] is False


def test_normalisation_modes():
    M = np.array([[1.0, 2.0], [3.0, 8.0]], np.float32)
    frame = render.normalize(M, "frame")
    assert np.allclose(frame.min(axis=0), 0) and np.allclose(frame.max(axis=0), 1)
    wl = render.normalize(M, "wavelength")
    assert np.allclose(wl.min(axis=1), 0) and np.allclose(wl.max(axis=1), 1)
    g = render.normalize(M, "global")
    assert g.min() == 0 and g.max() == 1


def test_colormaps_are_monotone_in_luminance():
    """亮度不单调的色标会让数值不同的区域看起来一样亮，人眼直接读错。"""
    for name in render.COLORMAPS:
        lut = render._lut(name).astype(float)
        y = 0.2126 * lut[:, 0] + 0.7152 * lut[:, 1] + 0.0722 * lut[:, 2]
        assert np.all(np.diff(y) >= -1.0), f"色标 {name} 的亮度不单调"


# ---------------------------------------------------------------- 派生曲线
def test_band_integral_is_trapezoid_not_naive_sum():
    lam = np.array([100.0, 110.0, 120.0])
    M = np.array([[1.0], [1.0], [1.0]])
    assert np.isclose(render.band_integral(M, lam, 100, 120)[0], 20.0)


def test_band_integral_is_continuous_in_the_band_edges():
    """边界挪一点点，结果也只该变一点点 —— 否则拖滑块时曲线会跳。"""
    lam, _, M = _synth(n_lam=400)
    a = render.band_integral(M, lam, 800, 900)
    b = render.band_integral(M, lam, 800, 900.4)
    assert np.abs(a - b).max() / np.abs(a).max() < 0.01


def test_slope_recovers_a_known_gradient():
    lam = np.linspace(900, 1000, 101)
    M = (3.0 * lam + 5.0)[:, None] * np.ones((1, 4))
    assert np.allclose(render.wavelength_slope(M, lam, 950, 20), 3.0)


def test_too_narrow_band_returns_nan_not_garbage():
    lam, _, M = _synth()
    assert np.all(np.isnan(render.band_integral(M, lam, 700, 700.001)))
    assert np.all(np.isnan(render.wavelength_slope(M, lam, 700, 0.001)))


def test_pick_frames_keeps_endpoints():
    t = np.linspace(0, 10, 500)
    idx = render.pick_frames(t, 20)
    assert idx[0] == 0 and idx[-1] == 499 and len(idx) <= 20
