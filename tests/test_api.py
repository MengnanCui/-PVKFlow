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


# ------------------------------------------------------------------ 光谱矩阵
@pytest.fixture()
def matrix_client(client, tmp_path):
    """导入一个真的光谱矩阵。"""
    import numpy as np

    lam = np.linspace(600, 1100, 240)
    t = np.linspace(0, 20, 80)
    src = tmp_path / "insitu"
    src.mkdir()
    p = src / "B99_M1_absorbance.csv"
    with open(p, "w", encoding="utf-8") as f:
        f.write("# Instrument: TestSpec\n")
        f.write("Wavelength(nm)," + ",".join(f"{x:.4f}" for x in t) + "\n")
        for i, w in enumerate(lam):
            row = 0.6 + 0.2 * np.cos(2 * np.pi * 2 * 4000 * (1 - 0.5 * t / 20) / w)
            f.write(f"{w:.4f}," + ",".join(f"{v:.6f}" for v in row) + "\n")

    scan = client.post("/api/files/scan", json={"path": str(src)}).json()
    client.post("/api/files/import", json={"files": scan["files"]})
    client.matrix_id = client.get(
        "/api/files", params={"q": "absorbance"}).json()["rows"][0]["artifact_id"]
    return client


def test_spectra_samples_finds_the_matrix(matrix_client):
    d = matrix_client.get("/api/spectra/samples").json()
    assert d["with_matrix"] >= 1
    hit = next(s for s in d["samples"] if s["matrices"])
    assert hit["matrices"][0]["columns_hint"] == 81


def test_spectra_meta(matrix_client):
    d = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/meta").json()
    assert d["n_lambda"] == 240 and d["n_time"] == 80
    assert d["meta"]["Instrument"] == "TestSpec"
    assert d["sample"]["name"] == "M1"
    assert d["frames_lambda_step"] > 0


def test_frames_json_and_binary_agree(matrix_client):
    import numpy as np

    info = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/frames").json()
    L, T = info["shape"]
    assert len(info["lambda"]) == L and len(info["time"]) == T

    raw = matrix_client.get(info["data_url"]).content
    arr = np.frombuffer(raw, dtype="<f4")
    assert arr.size == L * T, "二进制长度必须和 shape 对得上，否则前端会错位"


def test_heatmap_renders_png_with_axis_headers(matrix_client):
    r = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/heatmap.png",
                          params={"axis": "wavenumber"})
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    # header 只能是 latin-1，所以里面只放数字
    assert float(r.headers["X-Axis-Y-Min"]) > 0
    assert r.headers["X-Render-Info"]


def test_heatmap_rejects_a_too_narrow_band(matrix_client):
    r = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/heatmap.png",
                          params={"lam_min": 900, "lam_max": 900.5})
    assert r.status_code == 400
    assert r.json()["error"]["kind"] == "band_too_narrow"


def test_curve_endpoints(matrix_client):
    integ = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/curve",
                              params={"kind": "integral", "lam_min": 700,
                                      "lam_max": 900}).json()
    assert integ["n_points"] == 80 and all(v is not None for v in integ["y"])

    slope = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/curve",
                              params={"kind": "slope", "center": 800}).json()
    assert slope["n_points"] == 80


def test_thickness_endpoint_returns_a_real_curve(matrix_client):
    """膜厚算法已接入（fringe-optical-thickness 冻结规范的可执行副本）。"""
    r = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/thickness")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["x"]) == len(d["y"]) == d["n_points"] > 0
    assert d["unit"] == "nm"
    # 每一点都要带可信性标志 —— 只给数字不给判据是不行的
    assert len(d["flags"]) == len(d["status"]) == d["n_points"]
    assert set(d["status"]) <= {"OK", "DEGRADED", "LOW_CYCLES",
                                "LOW_SNR", "UNDERSAMPLED"}
    # 分辨率诊断（规范 §4 STEP 3）
    assert d["diagnostics"]["ot_floor_nm"] > 0
    assert d["diagnostics"]["bin_f_nm"] > 0
    # §5 的块 A–D 全文，四块一个都不能少
    for block in ("参数（本次运行实际使用）", "分辨率诊断",
                  "光学厚度结果", "必读声明"):
        assert block in d["report"]


def test_thickness_window_defaults_to_the_platform_band(matrix_client):
    """默认窗口是 775–1120，不是规范 DEFAULTS 里的 780–1050。

    冻结的默认值没动，平台是用 override 传的 —— 块 A 回显本次实际用的值。
    """
    from app.analysis import fringe_ot

    r = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/thickness")
    assert r.json()["diagnostics"]["window_nm"] == [775.0, 1120.0]
    assert fringe_ot.DEFAULTS["window_nm"] == [780, 1050]


def test_thickness_rejects_an_inverted_band(matrix_client):
    r = matrix_client.get(f"/api/spectra/{matrix_client.matrix_id}/thickness",
                          params={"lam_min": 1120, "lam_max": 775})
    assert r.status_code == 400
    assert r.json()["error"]["kind"] == "bad_band"


def test_non_matrix_file_gives_a_readable_error(matrix_client):
    """把一个两列文件丢给光谱接口，要给人话而不是 500。"""
    r = matrix_client.post("/api/files/upload", files=[
        ("files", ("B99_M2_jv.csv", b"Voltage,Current\n0,1\n0.1,2\n0.2,3\n0.3,4\n", "text/csv"))])
    aid = r.json()["imported"][0]["artifact_id"]

    r = matrix_client.get(f"/api/spectra/{aid}/meta")
    assert r.status_code == 400
    assert r.json()["error"]["kind"] in ("not_a_matrix", "parse_failed")
    assert "不像光谱矩阵" in r.json()["error"]["message"]


# ------------------------------------------------------------------ 一键配模型
def test_simple_model_form_saves_a_usable_provider(client, tmp_path, monkeypatch):
    """三个框存一个 provider —— 绝大多数人只有一个网关一个模型。"""
    r = client.post("/api/settings/models/simple", json={
        "name": "公司网关", "base_url": "https://gw.example.com/v1",
        "api_key": "sk-test-not-a-real-key", "model_id": "Qwen3.6-27B"})
    assert r.status_code == 200

    st = client.get("/api/assist/status").json()
    assert st["model_configured"] is True
    assert st["active"] == {"provider": "公司网关", "model": "Qwen3.6-27B"}


def test_simple_model_form_never_returns_the_key(client):
    """密钥只能打码回传。设置页和任何接口都不该看到原文。"""
    r = client.post("/api/settings/models/simple", json={
        "name": "g", "base_url": "https://gw.example.com/v1",
        "api_key": "sk-secret-value-1234", "model_id": "m"})
    assert "sk-secret-value-1234" not in r.text
    assert "sk-secret-value-1234" not in client.get("/api/settings/models").text


def test_blank_key_keeps_the_stored_one(client):
    """★ 改地址不该被迫把密钥再贴一遍。

    界面上只显示打码后的密钥，用户手里也未必还有原文 —— 留空必须是
    「不改」，而不是「清空」。清空的话下一次问模型会莫名其妙 401。
    """
    from app.ai import openai_compat

    client.post("/api/settings/models/simple", json={
        "name": "g", "base_url": "https://old.example.com/v1",
        "api_key": "sk-keep-me-1234", "model_id": "m1"})
    # 只改地址和模型名，密钥留空
    client.post("/api/settings/models/simple", json={
        "name": "g", "base_url": "https://new.example.com/v1",
        "api_key": "", "model_id": "m2"})

    cfg = openai_compat.load_config()["providers"]["g"]
    assert cfg["apiKey"] == "sk-keep-me-1234"
    assert cfg["baseUrl"] == "https://new.example.com/v1"
    assert cfg["models"][0]["id"] == "m2"


def test_first_time_without_a_key_is_refused(client):
    r = client.post("/api/settings/models/simple", json={
        "name": "全新的", "base_url": "https://x.example.com/v1",
        "api_key": "", "model_id": "m"})
    assert r.status_code == 400
    assert "密钥" in r.json()["error"]["message"]


def test_simple_model_form_rejects_junk(client):
    bad = [
        {"base_url": "", "model_id": "m", "api_key": "k"},
        {"base_url": "不是个网址", "model_id": "m", "api_key": "k"},
    ]
    for body in bad:
        assert client.post("/api/settings/models/simple", json=body).status_code == 400

    # 「一个模型都没选」**不再**是错误。自建网关很多不实现 /models，
    # 拉不到列表就连地址都存不下来，是最没道理的一种拦法。
    # 详见 test_an_address_can_be_saved_before_any_model_is_picked。
    ok = client.post("/api/settings/models/simple",
                     json={"base_url": "https://x/v1", "model_id": "", "api_key": "k"})
    assert ok.status_code == 200
