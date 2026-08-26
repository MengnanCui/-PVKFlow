"""API 层：端点连通性与统一的错误结构。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(workspace, sample_dir):
    from app.main import app
    from app.skills.registry import registry
    registry.load_all()
    with TestClient(app) as c:
        c.sample_dir = sample_dir
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_errors_have_a_consistent_shape(client):
    r = client.get("/api/files/nope")
    assert r.status_code == 404
    err = r.json()["error"]
    assert set(err) == {"message", "kind", "detail"}
    assert err["kind"] == "not_found"


def test_full_chain_scan_import_run_query(client):
    scan = client.post("/api/files/scan", json={"path": str(client.sample_dir)}).json()
    assert scan["count"] == 4

    imported = client.post("/api/files/import", json={"files": scan["files"]}).json()
    assert imported["counts"]["imported"] == 4

    files = client.get("/api/files", params={"q": "jv"}).json()
    aid = files["rows"][0]["artifact_id"]

    preview = client.get(f"/api/artifacts/{aid}/preview").json()
    assert preview["kind"] == "table"
    assert "Voltage(V)" in preview["columns"]

    suggest = client.post("/api/skills/suggest", json={"artifact_ids": [aid]}).json()
    assert suggest["source"] == "rule" and suggest["suggestions"]

    run = client.post("/api/skills/run",
                      json={"skill_id": "table.preview", "artifact_ids": [aid]}).json()
    assert run["saved"] is True and run["metrics_written"] > 0

    detail = client.get(f"/api/runs/{run['analysis_run_id']}").json()
    assert detail["run"]["status"] == "ok" and detail["results"]

    assert client.get("/api/results/fields").json()["fields"]
    assert client.get("/api/overview").json()["counts"]["results"] > 0
    assert client.get("/api/storage/stats").json()["by_mode"]


def test_run_without_files_is_rejected(client):
    r = client.post("/api/skills/run", json={"skill_id": "table.preview", "artifact_ids": []})
    assert r.status_code == 400
    assert "选择" in r.json()["error"]["message"]


def test_unready_skill_returns_a_clear_error(client):
    scan = client.post("/api/files/scan", json={"path": str(client.sample_dir)}).json()
    client.post("/api/files/import", json={"files": scan["files"]})
    aid = client.get("/api/files", params={"q": "jv"}).json()["rows"][0]["artifact_id"]

    r = client.post("/api/skills/run",
                    json={"skill_id": "thickness.generic", "artifact_ids": [aid]})
    assert r.status_code == 500
    assert "算法尚未接入" in r.json()["error"]["message"]


def test_ask_without_a_model_says_so(client):
    r = client.post("/api/assist/ask", json={"question": "这是什么"})
    assert r.status_code == 501
    assert r.json()["error"]["kind"] == "no_model"


def test_assist_status_admits_no_model(client):
    s = client.get("/api/assist/status").json()
    assert s["model_configured"] is False
    assert s["rules_available"] is True      # 规则引擎永远可用


def test_settings_rejects_unknown_keys(client):
    r = client.post("/api/settings", json={"secret_backdoor": 1})
    assert r.status_code == 400


def test_settings_roundtrip(client):
    client.post("/api/settings", json={"naming_rules": ["{sample}"]})
    assert client.get("/api/settings").json()["settings"]["naming_rules"] == ["{sample}"]


def test_models_config_must_have_providers(client):
    assert client.post("/api/settings/models", json={"config": "not json"}).status_code == 400
    assert client.post("/api/settings/models", json={"config": {}}).status_code == 400


def test_saved_model_config_masks_the_key(client):
    r = client.post("/api/settings/models", json={"config": {
        "providers": {"p": {"baseUrl": "http://x/v1", "apiKey": "sk-supersecret-9999",
                            "models": [{"id": "m"}]}}}})
    assert r.status_code == 200
    assert "sk-supersecret-9999" not in r.text


def test_upload_channel_copies(client):
    r = client.post("/api/files/upload",
                    files=[("files", ("B12_S4_x.csv", b"a,b\n1,2\n", "text/csv"))])
    assert r.json()["counts"]["imported"] == 1
    row = client.get("/api/files", params={"q": "S4"}).json()["rows"][0]
    assert row["storage_mode"] == "copied"


def test_browse_refuses_missing_path(client):
    assert client.get("/api/files/browse", params={"path": "/definitely/not/here"}).status_code == 404


def test_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "HTE Studio" in r.text


def test_unknown_api_path_returns_json_not_html(client):
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert r.json()["error"]["kind"] == "not_found"


def test_unknown_page_path_falls_back_to_the_app(client):
    r = client.get("/some/deep/route")
    assert r.status_code == 200 and "HTE Studio" in r.text
