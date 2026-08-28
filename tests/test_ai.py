"""AI 层：规则引擎必须永远可用；模型缺席时要明确说出来，不能假装。"""
import json

import pytest

from app.ai import openai_compat, rules
from app.ai.provider import NullProvider, ProviderUnavailable, extract_json
from app.storage import ingest


@pytest.fixture()
def imported(workspace, sample_dir):
    from app.skills.registry import registry
    registry.load_all()
    ingest.ingest_paths(ingest.scan_preview(sample_dir)["files"])
    from app.storage import db
    return db


# ---------------------------------------------------------------- 规则引擎
def test_rules_work_without_any_model(imported):
    aid = imported.scalar("SELECT artifact_id FROM artifact WHERE filename LIKE '%jv.csv'")
    out = rules.assist([aid])

    assert out["source"] == "rule"
    assert out["files"][0]["kind"] == "table"
    assert out["files"][0]["domain"] == "jv"      # 从列名认出来的
    assert out["suggestions"], "应该给出候选 skill"


def test_identify_recognises_images(imported):
    aid = imported.scalar("SELECT artifact_id FROM artifact WHERE filename LIKE '%.png'")
    from app.storage import artifacts
    info = rules.identify(artifacts.local_path(aid))
    assert info["kind"] == "image"
    assert info["width"] == 120 and info["height"] == 90


def test_inspect_frame_flags_real_problems():
    import pandas as pd

    df = pd.DataFrame({"a": [1, 1, 1, 1], "b": [1.0, 2.0, 3.0, 500.0]})
    messages = " ".join(i["message"] for i in rules.inspect_frame(df))
    assert "常量" in messages          # a 列没有信息量

    empty = rules.inspect_frame(pd.DataFrame())
    assert empty[0]["level"] == "error"


# ---------------------------------------------------------------- Provider
def test_null_provider_explains_itself():
    with pytest.raises(ProviderUnavailable) as exc:
        NullProvider().chat([])
    assert "设置" in str(exc.value)      # 告诉用户去哪儿配


def test_no_config_means_no_models(workspace):
    assert openai_compat.list_models() == []
    with pytest.raises(ProviderUnavailable):
        openai_compat.resolve()


def test_config_is_parsed_in_the_users_format(workspace):
    from app import config

    config.PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PROVIDERS_PATH.write_text(json.dumps({
        "providers": {
            "vllm-local": {
                "baseUrl": "http://example.invalid/v1",
                "api": "openai-completions",
                "apiKey": "sk-secret-value-1234",
                "compat": {"supportsDeveloperRole": False},
                "models": [{"id": "Qwen3.6-27B", "name": "Qwen", "input": ["text", "image"],
                            "contextWindow": 101072, "maxTokens": 65535}],
            }
        }
    }), encoding="utf-8")

    models = openai_compat.list_models()
    assert [m.id for m in models] == ["Qwen3.6-27B"]
    assert models[0].context_window == 101072
    assert "image" in models[0].inputs

    provider, model_id = openai_compat.resolve()
    assert provider.name == "vllm-local" and model_id == "Qwen3.6-27B"


def test_api_key_is_never_returned_in_full(workspace):
    from app import config

    config.PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PROVIDERS_PATH.write_text(json.dumps({
        "providers": {"p": {"baseUrl": "http://x/v1", "apiKey": "sk-secret-value-1234",
                            "models": [{"id": "m"}]}}
    }), encoding="utf-8")

    described = json.dumps(openai_compat.describe_config(), ensure_ascii=False)
    assert "sk-secret-value-1234" not in described
    assert "…" in described


def test_developer_role_downgrades_when_unsupported():
    from app.ai.provider import ChatMessage

    p = openai_compat.OpenAICompatProvider("x", {
        "baseUrl": "http://x/v1", "models": [{"id": "m"}],
        "compat": {"supportsDeveloperRole": False},
    })
    assert p._normalize([ChatMessage("developer", "hi")])[0]["role"] == "system"

    q = openai_compat.OpenAICompatProvider("x", {
        "baseUrl": "http://x/v1", "models": [{"id": "m"}],
        "compat": {"supportsDeveloperRole": True},
    })
    assert q._normalize([ChatMessage("developer", "hi")])[0]["role"] == "developer"


def test_test_connection_reports_failure_without_raising(workspace):
    from app import config

    config.PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PROVIDERS_PATH.write_text(json.dumps({
        "providers": {"dead": {"baseUrl": "http://127.0.0.1:1/v1", "models": [{"id": "m"}]}}
    }), encoding="utf-8")

    result = openai_compat.test_connection()
    assert result["ok"] is False and result["error"]


# ---------------------------------------------------------------- JSON 抽取
@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    '好的，结果如下：\n{"a": 1}\n希望有帮助',
])
def test_extract_json_survives_model_chatter(raw):
    assert extract_json(raw) == {"a": 1}


def test_extract_json_gives_up_loudly():
    with pytest.raises(ValueError):
        extract_json("完全没有 JSON")


def test_a_non_json_reply_says_what_probably_happened(monkeypatch, tmp_path):
    """★ 公司网络上最常见的失败：代理/SSO 回了个登录页，状态码还是 200。

    不处理的话用户看到的是「Expecting value: line 1 column 1」——
    这句话没有告诉他任何可以行动的东西。
    """
    import httpx
    from app.ai import openai_compat
    from app.ai.provider import ChatMessage, ProviderUnavailable

    class FakeResp:
        status_code = 200
        text = "<html><body>请先登录公司网络</body></html>"
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    prov = openai_compat.OpenAICompatProvider(
        "网关", {"baseUrl": "https://x/v1", "apiKey": "k", "models": [{"id": "m"}]})

    with pytest.raises(ProviderUnavailable) as e:
        prov.chat([ChatMessage("user", "hi")])
    msg = str(e.value)
    assert "不是 JSON" in msg
    assert "登录页" in msg          # 提示了最可能的原因
    assert "请先登录公司网络" in msg  # 也把网关原话带上了


def test_connection_test_never_raises(monkeypatch, workspace):
    """「测试连接」是排查连不上的唯一工具，它自己不能崩。"""
    from app.ai import openai_compat

    def boom(*a, **k):
        raise RuntimeError("什么奇怪的错误")

    openai_compat.save_config({"providers": {"g": {
        "baseUrl": "https://x/v1", "apiKey": "k", "models": [{"id": "m"}]}}})
    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "chat", boom)

    r = openai_compat.test_connection()
    assert r["ok"] is False
    assert "什么奇怪的错误" in r["error"]


# ---------------------------------------------------------------- 模型发现
def _fake_models_response(monkeypatch, *, status=200, body=None, text=None, capture=None):
    """假装网关的 GET /models。不打网络。"""
    class _Resp:
        status_code = status
        def __init__(self):
            self.text = text if text is not None else json.dumps(body or {})
        def json(self):
            if text is not None:
                raise ValueError("not json")
            return body or {}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            if capture is not None:
                capture["url"] = url
                capture["headers"] = headers or {}
            return _Resp()

    monkeypatch.setattr(openai_compat.httpx, "Client", _Client)


def test_discover_lists_models_so_you_do_not_have_to_type_the_name(monkeypatch):
    cap = {}
    _fake_models_response(monkeypatch, body={"data": [
        {"id": "qwen-b", "owned_by": "local"}, {"id": "qwen-a"}]}, capture=cap)

    got = openai_compat.list_remote_models("http://gw.example/v1/", "sk-xyz")
    assert [m["id"] for m in got] == ["qwen-a", "qwen-b"]      # 排过序
    assert cap["url"] == "http://gw.example/v1/models"
    assert cap["headers"]["Authorization"] == "Bearer sk-xyz"


def test_a_gateway_without_a_models_endpoint_says_you_can_still_type_it(monkeypatch):
    """不是每个网关都实现 /models。这是常见情况，不是故障 ——
    错误里必须给出下一步，否则用户会以为整个配置流程坏了。"""
    _fake_models_response(monkeypatch, status=404, body={})
    with pytest.raises(ProviderUnavailable) as e:
        openai_compat.list_remote_models("http://gw.example/v1", "k")
    assert "手动填模型名" in str(e.value)


def test_a_login_page_is_named_as_such_not_reported_as_bad_json(monkeypatch):
    """公司网络上代理/SSO 回一个登录页、状态码还是 200。
    直接抛 JSONDecodeError 的话用户看到「Expecting value: line 1 column 1」，
    什么信息都没有。"""
    _fake_models_response(monkeypatch, text="<html><body>请先登录</body></html>")
    with pytest.raises(ProviderUnavailable) as e:
        openai_compat.list_remote_models("http://gw.example/v1", "k")
    msg = str(e.value)
    assert "不是 JSON" in msg and ("代理" in msg or "登录页" in msg)


def test_a_rejected_key_says_so_instead_of_dumping_the_status_code(monkeypatch):
    _fake_models_response(monkeypatch, status=401, body={})
    with pytest.raises(ProviderUnavailable) as e:
        openai_compat.list_remote_models("http://gw.example/v1", "wrong")
    assert "密钥" in str(e.value)


def test_presets_never_contain_a_private_gateway():
    """预设里只放公共服务商。内网地址和密钥一样，属于本机配置，
    进了仓库就等于泄露。"""
    blob = json.dumps(openai_compat.PRESETS, ensure_ascii=False)
    for url in (p["base_url"] for p in openai_compat.PRESETS):
        assert url.startswith(("https://", "http://127.0.0.1")), url
    assert "sk-" not in blob


def test_discover_uses_the_saved_key_and_never_returns_it(workspace, monkeypatch):
    """密钥留空 = 沿用存着的那个；而返回里一个字段都不带密钥。"""
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app

    config.PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PROVIDERS_PATH.write_text(json.dumps({
        "providers": {"gw": {"baseUrl": "http://gw.example/v1",
                             "apiKey": "sk-secret-value-1234",
                             "models": [{"id": "m"}]}}}), encoding="utf-8")

    cap = {}
    _fake_models_response(monkeypatch, body={"data": [{"id": "m2"}]}, capture=cap)
    with TestClient(app) as c:
        r = c.post("/api/settings/models/discover",
                   json={"base_url": "http://gw.example/v1"})
    assert r.status_code == 200
    # 用上了存着的密钥……
    assert cap["headers"]["Authorization"] == "Bearer sk-secret-value-1234"
    assert r.json()["used_saved_key"] is True
    # ……但一个字都没回传
    assert "sk-secret-value-1234" not in r.text
    assert [m["id"] for m in r.json()["models"]] == ["m2"]


def test_several_models_can_be_saved_at_once(workspace):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/settings/models/simple", json={
            "name": "网关", "base_url": "http://gw.example/v1",
            "api_key": "sk-abcdef123456", "model_ids": ["a", "b", "b", "c"]})
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["providers"][0]["models"]]
    assert ids == ["a", "b", "c"]            # 去重且保序
    assert "sk-abcdef123456" not in r.text   # 存进去了，但不回传
