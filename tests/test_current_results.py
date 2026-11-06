"""关键结果的「当前值」（自查 §6 #2）。

key_result 只追加：每跑一次批处理，每个样品再多一整套字段。原来 AI 上下文、
按字段筛选、字段列表都直接读整张表，新旧几次混在一起 —— 审查里复现过：
旧 run 写 9.0、新 run 写 1.0，AI 拿到的是 9.0。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def two_runs(workspace):
    """同一个样品跑了两次：第一次 OT 末值 9000，第二次 1000。另一个样品只跑过一次。"""
    from app.storage import db, results

    now = db.now()
    with db.tx() as c:
        for sid, name in (("smp_a", "A"), ("smp_b", "B")):
            c.execute("INSERT INTO sample(sample_id, name, batch, created_at) VALUES(?,?,?,?)",
                      (sid, name, "ZG", now))
    old = results.start_run("batch.abs_thickness", "0.2.0", sample_id="smp_a")
    results.write_results(old, [{"field_name": "ot_last_ok", "value": 9000.0, "unit": "nm"},
                                {"field_name": "only_old", "value": 1.0}], sample_id="smp_a")
    other = results.start_run("batch.abs_thickness", "0.2.0", sample_id="smp_b")
    results.write_results(other, [{"field_name": "ot_last_ok", "value": 5000.0, "unit": "nm"}],
                          sample_id="smp_b")
    new = results.start_run("batch.abs_thickness", "0.2.0", sample_id="smp_a")
    results.write_results(new, [{"field_name": "ot_last_ok", "value": 1000.0, "unit": "nm"}],
                          sample_id="smp_a")
    return {"old": old, "new": new}


def test_ai_sees_the_latest_value_not_an_old_one(two_runs):
    from app.ai import context

    facts = context._scalars_for(["smp_a"])
    assert facts["smp_a"]["ot_last_ok(nm)"] == 1000.0


def test_a_field_only_the_old_run_wrote_is_still_current(two_runs):
    """新的一次没写这个字段，旧值就仍是它的当前值 —— 不能凭空消失。"""
    from app.ai import context

    assert context._scalars_for(["smp_a"])["smp_a"]["only_old"] == 1.0


def test_field_filter_does_not_match_on_a_stale_value(two_runs):
    """「OT 末值大于 8000 的样品」—— A 现在是 1000，不该因为历史上的 9000 被筛进来。"""
    from app.storage import selection

    flt = selection.normalize({"field": [{"name": "ot_last_ok", "min": 8000}]})
    assert selection.count(flt) == 0
    flt = selection.normalize({"field": [{"name": "ot_last_ok", "max": 2000}]})
    assert selection.count(flt) == 1


def test_field_list_ranges_use_current_values(two_runs):
    from app.storage import results

    f = next(x for x in results.list_fields() if x["field_name"] == "ot_last_ok")
    assert f["n"] == 2                      # 两个样品各一个当前值，不是三条
    assert (f["min_v"], f["max_v"]) == (1000.0, 5000.0)


def test_rejecting_the_latest_falls_back_to_the_previous(two_runs):
    """「这次算坏了」不等于「这个样品没有值」。"""
    from app.ai import context
    from app.storage import db, results

    rid = db.scalar("SELECT id FROM key_result WHERE analysis_run_id=? AND field_name='ot_last_ok'",
                    (two_runs["new"],))
    results.set_quality(rid, "reject")
    assert context._scalars_for(["smp_a"])["smp_a"]["ot_last_ok(nm)"] == 9000.0


def test_history_is_still_there_when_asked_for(two_runs):
    from app.storage import results

    cur = results.query_results(field="ot_last_ok", sample_id="smp_a")
    hist = results.query_results(field="ot_last_ok", sample_id="smp_a", history=True)
    assert [r["value_num"] for r in cur["rows"]] == [1000.0]
    assert sorted(r["value_num"] for r in hist["rows"]) == [1000.0, 9000.0]


def test_per_sample_result_count_counts_fields_not_runs(two_runs):
    from app.storage import selection

    rows = selection.page(selection.normalize({}), limit=10)["rows"]
    n = {r["sample_id"]: r["n_results"] for r in rows}
    assert n["smp_a"] == 2                  # ot_last_ok + only_old，不是 3


def test_a_field_with_two_units_is_listed_once_and_flagged(workspace):
    """自查 §6 #28：字段列表按（字段, 单位）分组 —— 同一个字段换了单位就列两行，
    「N 个不同字段」也跟着多算；中文名是从组里随便挑的一行。"""
    from app.storage import db, results

    now = db.now()
    with db.tx() as c:
        for sid in ("smp_1", "smp_2", "smp_3"):
            c.execute("INSERT INTO sample(sample_id, name, created_at) VALUES(?,?,?)", (sid, sid, now))
    for sid, unit in (("smp_1", "nm"), ("smp_2", "nm"), ("smp_3", "µm")):
        rid = results.start_run("batch.abs_thickness", "0.2.0", sample_id=sid)
        results.write_results(rid, [{"field_name": "thickness", "label": "膜厚", "value": 500.0,
                                     "unit": unit}], sample_id=sid)
    rid = results.start_run("x", "1.0.0", sample_id="smp_1")
    results.write_results(rid, [{"field_name": "plain", "value": 1.0}], sample_id="smp_1")

    rows = [f for f in results.list_fields() if f["field_name"] == "thickness"]
    assert len(rows) == 1
    f = rows[0]
    assert f["units"] == ["nm", "µm"]           # 多的在前
    assert f["mixed_units"] is True and f["unit"] == "nm / µm"
    assert f["label"] == "膜厚" and f["n"] == 3 and f["n_numeric"] == 3

    plain = next(x for x in results.list_fields() if x["field_name"] == "plain")
    assert plain["label"] == "" and plain["units"] == [] and plain["mixed_units"] is False
