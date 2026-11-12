"""对比历史的改名 / 删除（app/batch_history.py，C077）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import batch, config, tasks
from app.storage import db
from tests.test_batch import _wait, imported  # noqa: F401  （夹具）


def _run(flt: dict) -> str:
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {"filter": flt, "recipe": {}, "title": "第一次"})["task_id"])
    return t["result"]["parent_run_id"]


@pytest.fixture()
def client(imported):  # noqa: F811
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_rename_changes_the_title_in_the_history(client):
    rid = _run({"batch": ["B21"]})
    r = client.patch(f"/api/batch/runs/{rid}", json={"title": "  B21 · 退火对照  "})
    assert r.status_code == 200 and r.json()["title"] == "B21 · 退火对照", r.text
    hist = {x["analysis_run_id"]: x for x in client.get("/api/batch/runs").json()["runs"]}
    assert '"title": "B21 · 退火对照"' in hist[rid]["params_json"]
    assert client.patch(f"/api/batch/runs/{rid}", json={"title": " "}).json()["error"]["kind"] == "bad_title"
    assert client.patch("/api/batch/runs/run_nope", json={"title": "x"}).status_code == 404


def test_delete_takes_the_run_its_results_tables_and_pins_and_current_values_fall_back(client):
    """删掉第二次：样品的当前值退回第一次算的；第一次、原始数据、样品都不动。"""
    first = _run({"batch": ["B21"]})
    second = _run({"batch": ["B21"]})
    pick = db.query_one("SELECT k.sample_id, k.field_name FROM key_result k JOIN analysis_run r USING(analysis_run_id)"
                        " WHERE r.parent_run_id=? LIMIT 1", (second,))
    cur_run = "SELECT r.parent_run_id FROM key_result_current k JOIN analysis_run r USING(analysis_run_id)" \
              " WHERE k.sample_id=? AND k.field_name=?"
    sid = (pick["sample_id"], pick["field_name"])
    assert db.scalar(cur_run, sid) == second
    with db.tx() as c:
        c.execute("INSERT INTO ai_pin(pin_id, analysis_run_id, note, created_at) VALUES('pin_x', ?, '分析', ?)",
                  (second, db.now()))
    tables = [config.WORKSPACE / r["path"] for r in
              db.query("SELECT path FROM data_table WHERE analysis_run_id=?", (second,))]
    assert tables and all(p.is_file() for p in tables)
    n_samples, n_raw = db.scalar("SELECT COUNT(*) FROM sample"), db.scalar("SELECT COUNT(*) FROM artifact")

    fp = client.get(f"/api/batch/runs/{second}/footprint").json()
    assert fp["n_samples"] == 4 and fp["n_results"] > 0 and fp["n_current"] == fp["n_results"]
    assert fp["n_pins"] == 1 and fp["n_tables"] == len(tables)

    r = client.delete(f"/api/batch/runs/{second}")
    assert r.status_code == 200 and r.json()["deleted"]["n_results"] == fp["n_results"], r.text
    assert not db.scalar("SELECT COUNT(*) FROM analysis_run WHERE analysis_run_id=? OR parent_run_id=?",
                         (second, second))
    assert not db.scalar("SELECT COUNT(*) FROM ai_pin WHERE analysis_run_id=?", (second,))
    assert not any(p.exists() for p in tables)
    assert db.scalar(cur_run, sid) == first                       # 当前值退回上一次
    assert client.get(f"/api/batch/runs/{first}").status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM sample") == n_samples
    assert db.scalar("SELECT COUNT(*) FROM artifact") == n_raw
    assert client.delete(f"/api/batch/runs/{second}").status_code == 404


def test_a_running_batch_cannot_be_deleted(client):
    from app.storage import results

    rid = results.start_run(batch.BATCH_SKILL_ID, batch.BATCH_VERSION)
    r = client.delete(f"/api/batch/runs/{rid}")
    assert r.status_code == 400 and r.json()["error"]["kind"] == "running"


def test_a_conversation_about_a_deleted_run_is_told_so(client):
    """对话的范围里记着那次的编号：删了以后 AI 拿到的是最近一次，而且被告知你问的那次没了 —— 不冒充「你正在看的」。"""
    from app.ai import context

    keep = _run({"batch": ["B21"]})
    gone = _run({"batch": ["B20"]})
    client.delete(f"/api/batch/runs/{gone}")
    facts = context.build({"mode": "all"}, run_id=gone)["facts"]
    assert "你正在看的这次对比" not in facts
    comp = facts["最近一次对比"]
    assert comp["运行号"] == keep and gone in comp["说明"]
