"""AI 上下文里的曲线摘要（P-44：你要求「每个样品的曲线下采样到几十个点一起给模型」）。"""
from __future__ import annotations

import numpy as np

from app import batch, tasks
from app.ai import context
from tests.test_batch import _wait, imported  # noqa: F401  —— 复用批处理那组样品


def _run(flt, recipe=None):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {"filter": flt, "recipe": recipe or {}})["task_id"])
    assert t["status"] == "ok", t
    return t["result"]["parent_run_id"]


def _samples_in(cs):
    return {k: v for k, v in cs.items() if k not in ("说明", "没有曲线的样品")}


def test_bin_mean_averages_segments_instead_of_picking_points():
    """分段取平均，不是抽点：抽点会漏掉夹在两点之间的平台和突变。"""
    t = np.arange(10, dtype=float)
    y = t.copy()
    y[3] = np.nan                                   # 缺失值不参与平均
    edges = np.linspace(0, 9, 4)                    # 3 段：[0,3) [3,6) [6,9]
    assert context._bin_mean(t, y, edges) == [1.0, 4.5, 7.5]
    # 一个有限值都没有的段写 None，格式化成 —
    assert context._fmt_series([1234.5678, None, 0.000123]) == "1235 — 0.000123"


def test_each_processed_sample_gets_its_curves(imported):
    rid = _run({"batch": ["B21"]})
    facts = context.build({"mode": "all", "filter": {"batch": ["B21"]}})["facts"]
    cs = facts["曲线摘要"]
    samples = _samples_in(cs)
    assert len(samples) == 4
    one = next(iter(samples.values()))
    assert one["来自对比"] == rid
    n_bins = len(one["时间 (s)"].split())
    assert context.CURVE_BINS_MIN <= n_bins <= context.CURVE_BINS_MAX
    for key in ("积分强度 (a.u.·nm)", "dI/dλ (a.u./nm)"):
        assert len(one[key].split()) == n_bins
    if "OT 可信比例" in one:
        vals = [float(v) for v in one["OT 可信比例"].split() if v != "—"]
        assert all(0.0 <= v <= 1.0 for v in vals)
    assert "分成" in cs["说明"] and "取平均" in cs["说明"]


def test_samples_never_batch_processed_are_listed_not_invented(imported):
    _run({"batch": ["B21"]})
    cs = context.build({"mode": "all", "filter": {"batch": ["B21", "B22"]}})["facts"]["曲线摘要"]
    assert len(_samples_in(cs)) == 4
    assert len(cs["没有曲线的样品"]) == 4 and all(x.startswith("B22/") for x in cs["没有曲线的样品"])


def test_the_latest_batch_is_the_one_shown(imported):
    """和「当前值」同一个口径：最后一次跑过它的批处理为准。"""
    _run({"batch": ["B21"]})
    second = _run({"batch": ["B21"]}, {"integral_min": 700, "integral_max": 900})
    cs = context.build({"mode": "all", "filter": {"batch": ["B21"]}})["facts"]["曲线摘要"]
    assert {v["来自对比"] for v in _samples_in(cs).values()} == {second}


def test_more_samples_means_fewer_segments_but_never_below_the_floor(imported, monkeypatch):
    _run({"has_matrix": True})
    monkeypatch.setattr(context, "CURVE_NUMBER_BUDGET", 50)
    cs = context.build({"mode": "all", "filter": {}})["facts"]["曲线摘要"]
    one = next(iter(_samples_in(cs).values()))
    assert len(one["时间 (s)"].split()) == context.CURVE_BINS_MIN


def test_no_curves_when_the_range_is_too_wide_to_read(imported):
    """超过逐条阅读上限时连样品明细都不给，曲线更不给 —— 先让模型提议收窄。"""
    _run({"has_matrix": True})
    built = context.build({"mode": "all", "filter": {}}, detail_max=3)
    assert built["needs_narrowing"] and "曲线摘要" not in built["facts"]


def test_curves_go_last_and_the_prompt_stays_bounded(imported):
    """事实包超长时从尾部截断 —— 曲线放最后，截也先截它，不截标量表。"""
    _run({"has_matrix": True})
    facts = context.build({"mode": "all", "filter": {}})["facts"]
    assert list(facts)[-1] == "曲线摘要"
    text = context.to_prompt(facts)
    assert len(text.encode("utf-8")) < context.FACTS_BYTE_LIMIT
    # 一条曲线是一行，不是 json 数组被 indent 拆成几十行
    assert "\n        7" not in text
