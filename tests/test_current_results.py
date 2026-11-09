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


# ---------------------------------------------------------------- 审核（D 组）
@pytest.fixture()
def api(two_runs):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c


def _id(run, field="ot_last_ok"):
    from app.storage import db
    return db.scalar("SELECT id FROM key_result WHERE analysis_run_id=? AND field_name=?", (run, field))


def test_rejecting_through_the_api_keeps_a_reason_and_says_what_is_current_now(api, two_runs):
    rid = _id(two_runs["new"])
    r = api.post(f"/api/results/{rid}/quality", json={"quality": "reject", "note": "那片样品裂了"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["result"]["quality"] == "reject" and d["result"]["quality_note"] == "那片样品裂了"
    assert d["result"]["quality_at"]                                   # 什么时候标的也记下了
    assert d["current"]["value_num"] == 9000.0                          # 当前值退回上一次
    assert d["current"]["analysis_run_id"] == two_runs["old"]
    # 再标回「可信」：当前值回到新的那次
    d = api.post(f"/api/results/{rid}/quality", json={"quality": "validated"}).json()
    assert d["current"]["value_num"] == 1000.0 and d["current"]["quality"] == "validated"


def test_bad_quality_or_missing_result_is_refused(api, two_runs):
    assert api.post(f"/api/results/{_id(two_runs['new'])}/quality",
                    json={"quality": "maybe"}).status_code == 400
    assert api.post("/api/results/999999/quality", json={"quality": "reject"}).status_code == 404


def test_several_at_once_is_all_or_nothing(api, two_runs):
    from app.storage import db

    ids = [_id(two_runs["new"]), _id(two_runs["old"])]
    r = api.post("/api/results/quality", json={"ids": ids + [999999], "quality": "reject"})
    assert r.status_code == 404
    assert db.scalar("SELECT COUNT(*) FROM key_result WHERE quality='reject'") == 0   # 一条都没改
    r = api.post("/api/results/quality", json={"ids": ids, "quality": "reject", "note": "整次测量都坏了"})
    assert r.json() == {"updated": 2}
    # 两次都作废了：这个样品这个字段就没有当前值了（不是退回一个坏数）
    fields = api.get("/api/results/by-sample/smp_a").json()["fields"]
    f = next(x for x in fields if x["field_name"] == "ot_last_ok")
    assert f["current"] is None and len(f["history"]) == 2


def test_a_sample_lists_current_and_history_per_field(api, two_runs):
    fields = {f["field_name"]: f for f in api.get("/api/results/by-sample/smp_a").json()["fields"]}
    ot = fields["ot_last_ok"]
    assert ot["current"]["value_num"] == 1000.0 and ot["current"]["is_current"]
    assert [h["value_num"] for h in ot["history"]] == [1000.0, 9000.0]   # 新的在前
    assert [h["is_current"] for h in ot["history"]] == [True, False]
    assert fields["only_old"]["current"]["value_num"] == 1.0


def test_the_relation_page_follows_a_rejection(api, two_runs):
    """构效关系页上作废一个坏数，散点里那个样品就用退回之后的值。"""
    from app.storage import results

    rid = results.start_run("x", "1.0.0", sample_id="smp_a")
    results.write_results(rid, [{"field_name": "jsc", "value": 20.0}], sample_id="smp_a")
    rid = results.start_run("x", "1.0.0", sample_id="smp_b")
    results.write_results(rid, [{"field_name": "jsc", "value": 21.0}], sample_id="smp_b")
    pts = {p["sample_id"]: p for p in api.post("/api/relation/pairs",
                                               json={"x": "ot_last_ok", "y": "jsc"}).json()["points"]}
    assert pts["smp_a"]["x"] == 1000.0 and pts["smp_a"]["x_quality"] == "review"
    api.post(f"/api/results/{pts['smp_a']['x_id']}/quality", json={"quality": "reject"})
    pts = {p["sample_id"]: p for p in api.post("/api/relation/pairs",
                                               json={"x": "ot_last_ok", "y": "jsc"}).json()["points"]}
    assert pts["smp_a"]["x"] == 9000.0
