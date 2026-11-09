"""构效关系：统计（app/analysis/stats.py）和接口（/api/relation/*）。

统计的参考值是在另一个装了 scipy 1.18.1 的环境里算的（scipy.stats.pearsonr / spearmanr /
t.sf、numpy.percentile / polyfit），原样抄在下面。平台本身不装 scipy（stats.py 开头说了为什么）。
"""
from __future__ import annotations

import math

import pytest

from app.analysis import stats

# ---------------------------------------------------------------- 统计 vs scipy
CASES = {
    "small_n8": ([1, 2, 3, 4, 5, 6, 7, 8], [2.1, 3.9, 6.2, 7.8, 10.1, 12.2, 13.8, 16.5]),
    "ties_n12": ([1, 2, 2, 3, 4, 4, 4, 5, 6, 7, 7, 8], [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8]),
    "neg_n40": ([i * 0.5 for i in range(40)], [100 - 2.0 * i + 7 * math.sin(i * 1.7) for i in range(40)]),
    "weak_n25": ([math.cos(i * 2.3) * 10 for i in range(25)],
                 [math.sin(i * 0.9 + 1) * 3 + 0.15 * i for i in range(25)]),
    "n4": ([1.0, 2.0, 4.0, 3.0], [1.5, 2.5, 3.0, 4.5]),
    "n3": ([1.0, 2.0, 3.0], [2.0, 1.0, 4.0]),
    "big_n300": ([math.sin(i * 0.37) * 50 + i * 0.1 for i in range(300)],
                 [math.sin(i * 0.37 + 0.3) * 40 + math.cos(i) * 20 for i in range(300)]),
}
SCIPY = {
    "small_n8": dict(r=0.9989776683093575, p=2.6692080418379015e-09, rho=1.0, ps=0.0, ci=[0.9941133052345015, 0.9998228106587458], fit=[2.0309523809523813, -0.06428571428571564]),  # noqa: E501
    "ties_n12": dict(r=0.486923517001912, p=0.10839477873963832, rho=0.5392857142857144, ps=0.07038002251471209, ci=[-0.12071006947313143, 0.8291283675547594], fit=[0.552352048558422, 1.8937784522003047]),  # noqa: E501
    "neg_n40": dict(r=-0.9783592920730859, p=1.3126974686156634e-27, rho=-0.977485928705441, ps=2.7628524456018336e-27, ci=[-0.98858078651994, -0.9591762268355526], fit=[-4.009777258909569, 100.21693136418915]),  # noqa: E501
    "weak_n25": dict(r=-0.007331729503248798, p=0.9722534109690485, rho=-0.050769230769230775, ps=0.809551014193721, ci=[-0.40130006484371317, 0.3889258847774795], fit=[-0.0023968937969505717, 1.9855487750338134]),  # noqa: E501
    "n4": dict(r=0.6713171133426189, p=0.328682886657381, rho=0.7999999999999999, ps=0.2000000000000001, ci=[-0.816700217599509, 0.9922258357183129], fit=[0.65, 1.2499999999999998]),  # noqa: E501
    "n3": dict(r=0.654653670707977, p=0.5456289483429901, rho=0.5, ps=0.6666666666666666, ci=None, fit=[0.9999999999999998, 0.3333333333333345]),  # noqa: E501
    "big_n300": dict(r=0.825966550343737, p=3.660494699142714e-76, rho=0.825004277825309, ps=7.70444369450891e-76, ci=[0.7862678127521069, 0.858874185524388], fit=[0.7220077099482406, -10.851039974262056]),  # noqa: E501
}
T_SF2 = {(0.1, 1): 0.936548965138893, (1.0, 2): 0.4226497308103742, (2.0, 5): 0.10193947882985835,
         (2.5, 10): 0.03144684423660882, (5.0, 38): 1.3276418057994204e-05,
         (12.0, 298): 2.485172146184408e-27, (1.0, 1): 0.5000000000000001,
         (12.0, 38): 1.705124000284518e-14, (2.0, 298): 0.04640757908343644}


def close(a, b, rel=1e-9):
    return a == pytest.approx(b, rel=rel, abs=1e-300)


@pytest.mark.parametrize("case", sorted(CASES))
def test_correlation_matches_scipy(case):
    x, y = CASES[case]
    got, ref = stats.correlation(x, y), SCIPY[case]
    assert got["n"] == len(x)
    assert close(got["pearson"]["r"], ref["r"]) and close(got["pearson"]["p"], ref["p"])
    assert close(got["spearman"]["rho"], ref["rho"]) and close(got["spearman"]["p"], ref["ps"])
    assert close(got["fit"]["slope"], ref["fit"][0]) and close(got["fit"]["intercept"], ref["fit"][1])
    if ref["ci"] is None:
        assert got["pearson"]["ci95"] is None            # n = 3：置信区间算不了
    else:
        assert close(got["pearson"]["ci95"][0], ref["ci"][0])
        assert close(got["pearson"]["ci95"][1], ref["ci"][1])


@pytest.mark.parametrize("t, df", sorted(T_SF2))
def test_t_distribution_tail_matches_scipy(t, df):
    assert close(stats.t_two_sided_p(t, df), T_SF2[(t, df)])


def test_ties_get_the_average_rank():
    assert list(stats.rankdata([10, 20, 20, 5, 20])) == [2.0, 4.0, 4.0, 1.0, 4.0]


def test_too_few_or_constant_gives_no_number_and_says_why():
    """不给一个看起来正常的数。"""
    few = stats.correlation([1, 2], [3, 4])
    assert few["pearson"] is None and "至少要 3 个" in few["why"]
    flat = stats.correlation([1, 2, 3, 4], [5, 5, 5, 5])
    assert flat["pearson"] is None and flat["spearman"] is None and "都一样" in flat["why"]


def test_nan_pairs_are_dropped_together():
    got = stats.correlation([1, 2, float("nan"), 4, 5], [2, 4, 6, float("inf"), 10])
    assert got["n"] == 3 and close(got["pearson"]["r"], 1.0)


def test_small_samples_flag_the_spearman_p_as_approximate():
    assert stats.correlation(*CASES["n4"])["spearman"]["approx"] is True
    assert stats.correlation(*CASES["neg_n40"])["spearman"]["approx"] is False


def test_box_stats_quartiles_whiskers_and_outliers():
    b = stats.box_stats([1, 2, 3, 4, 100])
    assert (b["q1"], b["median"], b["q3"]) == (2.0, 3.0, 4.0)        # numpy.percentile 线性插值
    assert (b["whisker_lo"], b["whisker_hi"]) == (1.0, 4.0)          # 须到界内最远的点
    assert b["outliers"] == [4]                                      # 100 在 Q3 + 1.5·IQR = 7 之外
    b = stats.box_stats([5.5, 3.2, 8.8, 1.1, 4.4, 9.9, 2.2])
    assert close(b["q1"], 2.7) and close(b["median"], 4.4) and close(b["q3"], 7.15)
    assert b["outliers"] == []
    assert stats.box_stats([float("nan")])["n"] == 0


# ---------------------------------------------------------------- 接口
@pytest.fixture()
def lab(workspace):
    """两个批次。B1 的 6 个样品 thick 和 jsc 都有、jsc 随 thick 线性涨；
    另有只有 thick 的、只有 jsc 的、什么都没有的各一个；
    B2 的 s21 跑过两次：旧值 999 已被新值盖掉。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.storage import db, results

    now = db.now()
    rows = [(f"s1{i}", f"S1{i}", "B1", 100.0 + 10 * i, 20.0 + 0.5 * i) for i in range(6)]
    rows += [("s21", "S21", "B2", 150.0, 30.0), ("s22", "S22", "B2", 160.0, 21.0),
             ("s23", "S23", "B2", 170.0, 22.0), ("s24", "S24", "B2", 180.0, 23.0),
             ("sx", "SX", "B2", 190.0, None), ("sy", "SY", "B2", None, 25.0),
             ("s0", "S0", "B2", None, None)]
    with db.tx() as c:
        for sid, name, batch, *_ in rows:
            c.execute("INSERT INTO sample(sample_id, name, batch, created_at) VALUES(?,?,?,?)",
                      (sid, name, batch, now))
    old = results.start_run("batch.abs_thickness", "0.2.0", sample_id="s21")
    results.write_results(old, [{"field_name": "jsc", "value": 999.0, "unit": "mA/cm2"}],
                          sample_id="s21")
    for sid, _n, _b, thick, jsc in rows:
        rid = results.start_run("batch.abs_thickness", "0.2.0", sample_id=sid)
        vals = []
        if thick is not None:
            vals.append({"field_name": "thick", "label": "膜厚", "value": thick, "unit": "nm"})
        if jsc is not None:
            vals.append({"field_name": "jsc", "label": "短路电流", "value": jsc, "unit": "mA/cm2"})
        if vals:
            results.write_results(rid, vals, sample_id=sid)
    with TestClient(app) as client:
        yield client


def test_pairs_take_current_values_and_count_what_is_missing(lab):
    r = lab.post("/api/relation/pairs", json={"x": "thick", "y": "jsc"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["points"]) == 10
    s21 = next(p for p in d["points"] if p["sample_id"] == "s21")
    assert s21["y"] == 30.0                                  # 当前值，不是旧的 999
    assert (d["n_scope"], d["n_only_x"], d["n_only_y"], d["n_neither"]) == (13, 1, 1, 1)
    assert d["x"]["label"] == "膜厚" and d["y"]["unit"] == "mA/cm2"
    ref = stats.correlation([p["x"] for p in d["points"]], [p["y"] for p in d["points"]])
    assert d["stats"]["pearson"]["r"] == pytest.approx(ref["pearson"]["r"])


def test_pairs_follow_the_same_filter_as_the_processing_page(lab):
    d = lab.post("/api/relation/pairs", json={"x": "thick", "y": "jsc",
                                              "filter": {"batch": ["B1"]}}).json()
    assert {p["batch"] for p in d["points"]} == {"B1"} and len(d["points"]) == 6
    assert d["stats"]["pearson"]["r"] == pytest.approx(1.0)  # B1 里是严格的直线
    assert d["stats"]["pearson"]["p"] == 0.0


def test_unknown_or_empty_fields_are_refused(lab):
    r = lab.post("/api/relation/pairs", json={"x": "thick", "y": "nope"})
    assert r.status_code == 400 and "没有这个字段" in r.json()["error"]["message"]
    assert lab.post("/api/relation/pairs", json={"x": "thick"}).status_code == 400


def test_mixed_units_are_reported(lab):
    from app.storage import results

    rid = results.start_run("x", "1.0.0", sample_id="s0")
    results.write_results(rid, [{"field_name": "thick", "value": 0.2, "unit": "µm"}], sample_id="s0")
    d = lab.post("/api/relation/pairs", json={"x": "thick", "y": "jsc"}).json()
    assert d["x"]["mixed_units"] is True and set(d["x"]["units"]) == {"nm", "µm"}


def test_groups_give_box_stats_per_batch_with_named_outliers(lab):
    d = lab.post("/api/relation/groups", json={"y": "jsc"}).json()
    names = [g["name"] for g in d["groups"]]
    assert names == ["B1", "B2"]
    b2 = d["groups"][1]
    assert b2["n"] == 5                                      # s21 s22 s23 s24 sy
    assert [o["name"] for o in b2["outliers"]] == ["S21"]    # 30 远在其余 21–25 之上
    assert d["n"] == 11
    bad = lab.post("/api/relation/groups", json={"y": "jsc", "by": "colour"})
    assert bad.status_code == 400
