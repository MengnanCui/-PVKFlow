"""真实仪器格式的 Data.csv。

这个格式和之前假设的宽表差别很大：tab 分隔、两个数据块、时间轴藏在块内。
下面每一条测的都是「猜错了会静默出错」的地方。
"""
import numpy as np
import pytest

from app.parsers import insitu_csv as ic


def write_data_csv(path, *, n_lam=40, n_t=12, absorption=True, origin=True,
                   time_row=True, ot=5000.0):
    """造一个最小但结构完整的 Data.csv。

    两个块的波长网格**故意不同** —— 真文件就是这样（Origin 从 322 起，
    Absorption 从 330 起），只解析一遍再切会切错。
    """
    t = np.round(np.linspace(0, 1.1 * (n_t - 1), n_t), 3)
    guids = [f"{i:032X}" for i in range(n_t)]
    clocks = [f"32:{50.3 + v:04.1f}" for v in t]
    pad = "\t" * n_t

    def block(name, lam):
        out = [f"{name} Wavelength\t" + "\t".join(guids),
               "采集时间\t" + "\t".join(clocks)]
        if time_row:
            out.append("相对第一帧时间(s)\t" + "\t".join(f"{v:g}" for v in t))
        for w in lam:
            row = 50 + 20 * np.cos(2 * np.pi * 2 * ot / w * (1 - 0.2 * t / t[-1]))
            out.append(f"{w:.3f}\t" + "\t".join(f"{v:g}" for v in row))
        return out

    lines = [
        f"Mode\tMode5{pad}", f"CollectionDuration(s)\t120{pad}",
        f"TaskDuration(s)\t{t[-1]:.4f}{pad}", f"MeasurementBrightPD\t2520{pad}",
        f"MeasurementDarkPD\t1448{pad}", f"ReferencePDDiff\t32{pad}",
        f"ReferencePDRatio\t750{pad}", f"PDDeltaFromReference\t-1.3866{pad}",
        f"PDRemainingRatio\t33.5{pad}", pad,
    ]
    if origin:
        lines += block("Origin", np.linspace(322.036, 1120.568, n_lam + 5))
    if absorption:
        lines += block("Absorption", np.linspace(330.276, 1120.568, n_lam))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def data_csv(tmp_path):
    return write_data_csv(tmp_path / "Data.csv")


# ---------------------------------------------------------------- 基本解析
def test_parses_the_absorption_block_by_default(data_csv):
    sm = ic.parse(data_csv)
    assert sm.meta["block"] == "Absorption"
    assert sm.shape == (40, 12)
    assert sm.lam[0] == pytest.approx(330.276)
    assert sm.lam[-1] == pytest.approx(1120.568)


def test_time_axis_comes_from_the_row_not_the_header(data_csv):
    """时间轴在块内的「相对第一帧时间(s)」那一行，不在表头。

    表头是一排 GUID —— 按宽表的老办法读表头，拿到的是一串 NaN。
    """
    sm = ic.parse(data_csv)
    assert sm.t[0] == 0.0
    assert sm.t[-1] == pytest.approx(1.1 * 11)
    assert np.all(np.diff(sm.t) > 0)


def test_two_blocks_have_different_wavelength_grids(data_csv):
    """两个块的波长网格不一样，必须各读各的。"""
    a = ic.parse(data_csv, block="Absorption")
    o = ic.parse(data_csv, block="Origin")
    assert a.lam[0] != o.lam[0]
    assert a.shape[0] != o.shape[0]


def test_header_fields_land_in_meta(data_csv):
    sm = ic.parse(data_csv)
    assert sm.meta["Mode"] == "Mode5"
    assert sm.meta["MeasurementBrightPD"] == 2520
    assert sm.meta["PDRemainingRatio"] == pytest.approx(33.5)


def test_absorption_is_percent_not_absorbance(data_csv):
    """这一列是 0–100 的百分比，对 T 线性。

    如果当成吸光度 A = -log10(T) 去还原，会在功率谱上生成 2f/3f 假峰
    （冻结规范 §2 / STEP 0）。所以这里必须明确标成 False。
    """
    sm = ic.parse(data_csv)
    assert sm.meta["input_is_absorbance"] is False
    assert sm.meta["value_kind"] == "absorption_percent"


# ---------------------------------------------------------------- 坏情况
def test_missing_absorption_block_fails_loudly(tmp_path):
    """没有 Absorption 块时**不能**拿 Origin 顶替。

    Origin 是原始 PD 计数，量纲和量级都不一样。静默替换的话，
    界面上一切正常，算出来的东西全是错的。
    """
    p = write_data_csv(tmp_path / "Data.csv", absorption=False)
    with pytest.raises(ic.InsituFormatError, match="没有 Absorption 块"):
        ic.parse(p)


def test_missing_time_row_fails_loudly(tmp_path):
    p = write_data_csv(tmp_path / "Data.csv", time_row=False)
    with pytest.raises(ic.InsituFormatError, match="相对第一帧时间"):
        ic.parse(p)


def test_not_a_data_csv_at_all(tmp_path):
    p = tmp_path / "Data.csv"
    p.write_text("随便写点什么\n不是这个格式\n", encoding="utf-8")
    with pytest.raises(ic.InsituFormatError, match="没有找到任何数据块"):
        ic.parse(p)


def test_short_rows_are_truncated_not_nan_padded(tmp_path):
    """某一行被写断时，按最短对齐，不补 NaN。

    补 NaN 会让下游的 FFT 见到不存在的采样点 —— 宁可少几帧。
    """
    p = write_data_csv(tmp_path / "Data.csv", n_t=12)
    lines = p.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Absorption Wavelength"))
    lines[start + 5] = "\t".join(lines[start + 5].split("\t")[:6])   # 砍成 5 帧
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sm = ic.parse(p)
    assert sm.shape[1] == 5
    assert np.isfinite(sm.M).all()


def test_trailing_empty_columns_are_ignored(data_csv):
    """抬头那几行后面跟着上百个空 tab，不能被当成数据列。"""
    sm = ic.parse(data_csv)
    assert sm.shape[1] == 12          # 不是 12 + 一堆空列


# ---------------------------------------------------------------- 识别与分流
def test_looks_like_insitu(data_csv, tmp_path):
    assert ic.looks_like_insitu(data_csv)
    plain = tmp_path / "other.csv"
    plain.write_text("Wavelength(nm),0.0,0.1\n400,1,2\n500,3,4\n", encoding="utf-8")
    assert not ic.looks_like_insitu(plain)


def test_matrix_parse_routes_to_the_insitu_parser(data_csv):
    """matrix.parse 是所有下游的统一入口，分流点在它里面。

    这样 npz 缓存、热力图、曲线全都不用知道有两种格式。
    """
    from app.parsers import matrix

    sm = matrix.parse(data_csv)
    assert sm.meta["source_format"] == "insitu_data_csv"
    assert sm.meta["block"] == "Absorption"


def test_block_names(data_csv):
    assert ic.block_names(data_csv) == ["Origin", "Absorption"]
