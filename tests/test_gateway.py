"""模型网关（app/ai/gateway.py，照 IFlow 改）。

网络一律换成 httpx.MockTransport：请求体、地址、头都记下来 —— 要看的正是「网关到底收到了什么」。
"""
import json

import httpx
import pytest

from app.agent import driver, tools
from app.ai import gateway
from app.ai.openai_compat import OpenAICompatProvider
from app.ai.provider import ChatMessage, ProviderUnavailable


class Net:
    """按顺序回话的假网关。reply 是 httpx.Response 或 (status, body, headers)。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        r = self.replies.pop(0) if self.replies else httpx.Response(500, text="剧本用完了")
        return r

    def body(self, i=-1) -> dict:
        return json.loads(self.seen[i].content)


@pytest.fixture()
def net(monkeypatch):
    slept: list[float] = []

    def install(*replies):
        n = Net(*replies)
        monkeypatch.setattr(gateway, "client_factory",
                            lambda timeout=None: httpx.Client(transport=httpx.MockTransport(n)))
        return n

    monkeypatch.setattr(gateway, "sleep", slept.append)
    gateway.reset_memo()
    driver.reset_learned()
    install.slept = slept
    yield install
    gateway.reset_memo()


def sse(*objs, event_names=False) -> httpx.Response:
    lines = []
    for o in objs:
        if event_names:
            lines.append(f"event: {o['type']}")
        lines.append(f"data: {json.dumps(o, ensure_ascii=False)}")
        lines.append("")
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="\n".join(lines) + "\n")


def anth(model="claude-sonnet-4-5", **cfg):
    return OpenAICompatProvider("A", {"baseUrl": "https://api.anthropic.com", "apiKey": "ak-1",
                                      "api": "anthropic-messages", "models": [{"id": model}], **cfg})


def oai(model="qwen3-32b", **cfg):
    return OpenAICompatProvider("O", {"baseUrl": "http://gw.test/v1", "apiKey": "sk-1",
                                      "models": [{"id": model}], **cfg})


# ------------------------------------------------------------------ Anthropic 协议
def test_anthropic_chat_uses_its_own_url_headers_and_system_field(net):
    n = net(httpx.Response(200, json={"content": [{"type": "text", "text": "你好"}], "usage": {"input_tokens": 3}}))
    r = anth().chat([ChatMessage("system", "你是助手"), ChatMessage("user", "hi"), ChatMessage("user", "再说一句")])
    assert r.text == "你好"
    req = n.seen[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "ak-1" and req.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req.headers
    b = n.body()
    assert b["system"] == "你是助手" and b["max_tokens"] == 4096
    # 相邻的两条 user 并成一条（Anthropic 要求交替）
    assert len(b["messages"]) == 1 and [x["text"] for x in b["messages"][0]["content"]] == ["hi", "再说一句"]


def test_anthropic_base_url_with_v1_is_not_doubled(net):
    n = net(httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]}))
    OpenAICompatProvider("A", {"baseUrl": "https://gw.test/v1", "api": "anthropic-messages",
                               "models": [{"id": "claude-x"}]}).chat([ChatMessage("user", "hi")])
    assert str(n.seen[0].url) == "https://gw.test/v1/messages"


def test_anthropic_stream_yields_text_deltas_only(net):
    net(sse({"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "想一想"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "膜厚"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "偏薄"}},
            {"type": "message_stop"}, event_names=True))
    assert list(anth().chat_stream([ChatMessage("user", "hi")])) == ["膜厚", "偏薄"]     # 思考不进正文


def test_agent_tool_use_over_anthropic(net):
    """智能体走 Anthropic：工具定义是 input_schema；tool_use 的参数是 input_json_delta 一截一截来的。"""
    n = net(sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "查一下"}},
                {"type": "content_block_start", "index": 1,
                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": "correlate"}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"x": "a",'}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ' "y": "b"}'}},
                {"type": "message_stop"}))
    history = [{"role": "user", "content": "甲乙相关吗"},
               {"role": "assistant", "content": "", "calls": [{"id": "c1", "name": "list_fields", "args": {}},
                                                               {"id": "c2", "name": "overview", "args": {}}]},
               {"role": "tool", "call_id": "c1", "name": "list_fields", "content": "{}"},
               {"role": "tool", "call_id": "c2", "name": "overview", "content": "{}"}]
    got = []
    s = driver.step(anth(), "claude-sonnet-4-5", "系统", history, tools.TOOLS, on_delta=got.append)
    assert s.text == "查一下" and got == ["查一下"]
    assert [(c.id, c.name, c.args) for c in s.calls] == [("toolu_1", "correlate", {"x": "a", "y": "b"})]
    b = n.body()
    assert b["system"] == "系统" and {t["name"] for t in b["tools"]} == set(tools.BY_NAME)
    assert "input_schema" in b["tools"][0] and "tool_choice" not in b
    # 两条工具结果并进同一条 user 消息，紧跟在 tool_use 后面
    assert [m["role"] for m in b["messages"]] == ["user", "assistant", "user"]
    assert [x["type"] for x in b["messages"][1]["content"]] == ["tool_use", "tool_use"]
    assert [x["tool_use_id"] for x in b["messages"][2]["content"]] == ["c1", "c2"]


# ------------------------------------------------------------------ 重试
def test_rate_limits_are_retried_with_backoff_and_retry_after(net):
    n = net(httpx.Response(429, headers={"retry-after": "2"}, text="slow down"),
            httpx.Response(503, text="busy"),
            httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]}))
    assert oai(model="m").chat([ChatMessage("user", "hi")]).text == "好"
    assert len(n.seen) == 3 and net.slept == [2.0, 2.0]       # 认 Retry-After；第二次按 1 s × 2¹


def test_500_is_not_retried_and_the_error_is_readable(net):
    n = net(httpx.Response(500, text="upstream exploded"))
    with pytest.raises(ProviderUnavailable, match="500"):
        oai(model="m").chat([ChatMessage("user", "hi")])
    assert len(n.seen) == 1 and net.slept == []


def test_retries_give_up_after_three(net):
    n = net(*[httpx.Response(502, text="bad gateway")] * 5)
    with pytest.raises(ProviderUnavailable, match="502"):
        oai(model="m").chat([ChatMessage("user", "hi")])
    assert len(n.seen) == 4 and net.slept == [1.0, 2.0, 4.0]


# ------------------------------------------------------------------ 思考参数
@pytest.mark.parametrize("model,protocol,off,on", [
    ("Qwen3-32B", gateway.OPENAI, {"chat_template_kwargs": {"enable_thinking": False}},
     {"chat_template_kwargs": {"enable_thinking": True}}),
    ("DeepSeek-V4-Flash", gateway.OPENAI, {"chat_template_kwargs": {"thinking": False}},
     {"chat_template_kwargs": {"thinking": True}}),
    ("Kimi-K2.6", gateway.OPENAI, {"chat_template_kwargs": {"thinking": False}},
     {"chat_template_kwargs": {"thinking": True}}),
    ("GLM-5.3-Flash", gateway.OPENAI, {"chat_template_kwargs": {"enable_thinking": False}},
     {"chat_template_kwargs": {"enable_thinking": True}}),
    ("glm-4.6", gateway.OPENAI, {"thinking": {"type": "disabled"}}, {"thinking": {"type": "enabled"}}),
    ("claude-sonnet-4-5", gateway.ANTHROPIC, {}, {"thinking": {"type": "enabled", "budget_tokens": 4096}}),
    ("gpt-5", gateway.OPENAI, {}, {"reasoning_effort": "medium"}),
    ("deepseek-reasoner", gateway.OPENAI, {}, {}),            # 恒定思考：什么都不发
    ("gpt-4o-mini", gateway.OPENAI, {}, {}),                  # 认不出写法：什么都不发
])
def test_thinking_fields_follow_the_model_family(model, protocol, off, on):
    assert gateway.thinking_fields(model, protocol, "off") == off
    assert gateway.thinking_fields(model, protocol, "on") == on
    assert gateway.thinking_fields(model, protocol, "default") == {}


def test_thinking_is_off_by_default_and_dropped_when_the_gateway_rejects_it(net):
    n = net(httpx.Response(400, text="unknown field chat_template_kwargs"),
            httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": "又好"}}]}))
    p = oai(model="qwen3-32b")
    assert p.thinking == "off"
    assert p.chat([ChatMessage("user", "hi")]).text == "好"
    assert "chat_template_kwargs" in n.body(0) and "chat_template_kwargs" not in n.body(1)
    p.chat([ChatMessage("user", "hi")])
    assert "chat_template_kwargs" not in n.body(2)          # 记住了，不再白试


def test_anthropic_thinking_on_raises_max_tokens_and_drops_temperature(net):
    n = net(httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]}))
    anth(thinking="on").chat([ChatMessage("user", "hi")], temperature=0.5)
    b = n.body()
    assert b["thinking"]["budget_tokens"] == 4096 and b["max_tokens"] > 4096 and "temperature" not in b


def test_a_reply_with_only_thinking_says_to_turn_it_off(net):
    net(httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "想了很久"}}]}))
    with pytest.raises(ProviderUnavailable, match="思考"):
        oai(model="m").chat([ChatMessage("user", "hi")])


# ------------------------------------------------------------------ 流式降级
def test_stream_rejected_falls_back_to_one_piece_and_is_remembered(net):
    n = net(httpx.Response(400, text="stream not supported"),
            httpx.Response(200, json={"choices": [{"message": {"content": "整包回答"}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": "第二次"}}]}))
    p = oai(model="m")
    assert list(p.chat_stream([ChatMessage("user", "hi")])) == ["整包回答"]
    assert n.body(0)["stream"] is True and "stream" not in n.body(1)
    assert list(p.chat_stream([ChatMessage("user", "hi")])) == ["第二次"]
    assert "stream" not in n.body(2)


def test_a_tools_rejection_does_not_blame_thinking(net):
    """网关不认 tools：交给驱动退到动作协议；思考参数不该被记成「被拒」。"""
    n = net(httpx.Response(400, text="tools is not supported"),
            sse({"choices": [{"delta": {"content": '{"say": "好", "calls": []}'}}]}))
    p = oai(model="qwen3-32b")
    s = driver.step(p, "qwen3-32b", "系统", [{"role": "user", "content": "hi"}], tools.TOOLS)
    assert s.kind == "action" and s.text == "好"
    assert "tools" in n.body(0) and "tools" not in n.body(1)
    assert n.body(1)["chat_template_kwargs"] == {"enable_thinking": False}   # 思考参数照发


# ------------------------------------------------------------------ 地址、列表、测速
def test_allowed_hosts(monkeypatch):
    monkeypatch.setenv("HTE_LLM_ALLOWED_HOSTS", "gw.ok.test")
    assert gateway.check_url("http://gw.ok.test/v1")
    with pytest.raises(ProviderUnavailable, match="允许列表"):
        gateway.check_url("http://evil.test/v1")
    with pytest.raises(ProviderUnavailable):
        gateway.check_url("file:///etc/passwd")


def test_family_dedupe_keeps_the_newest_of_each_family():
    kept, folded = gateway.dedupe_models(["Kimi-K2.5", "Kimi-K2.6", "DeepSeek-V4-Flash", "DeepSeek-V4-Pro",
                                          "DeepSeek-V3-Flash", "qwen3-32b"])
    assert kept == ["Kimi-K2.6", "DeepSeek-V4-Flash", "DeepSeek-V4-Pro", "qwen3-32b"]
    assert {"id": "Kimi-K2.5", "by": "Kimi-K2.6"} in folded
    assert {"id": "DeepSeek-V3-Flash", "by": "DeepSeek-V4-Flash"} in folded


def test_anthropic_model_list_uses_its_headers(net):
    n = net(httpx.Response(200, json={"data": [{"id": "claude-b"}, {"id": "claude-a"}]}))
    got = gateway.list_models("https://api.anthropic.com", "ak-1", gateway.ANTHROPIC)
    assert [m["id"] for m in got] == ["claude-a", "claude-b"]
    assert str(n.seen[0].url) == "https://api.anthropic.com/v1/models" and n.seen[0].headers["x-api-key"] == "ak-1"


def test_ping_never_raises(net):
    net(httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
        httpx.Response(401, text="bad key"))
    good = gateway.ping("http://gw.test/v1", "k", gateway.OPENAI, "m1")
    bad = gateway.ping("http://gw.test/v1", "k", gateway.OPENAI, "m2")
    assert good["ok"] and good["reply"] == "ok" and good["ms"] >= 0
    assert not bad["ok"] and "401" in bad["error"]


# ------------------------------------------------------------------ 设置接口
@pytest.fixture()
def client(workspace):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_settings_store_protocol_and_thinking_and_probe_with_the_saved_key(client, net):
    r = client.post("/api/settings/models/simple", json={
        "name": "Claude", "base_url": "https://api.anthropic.com", "api_key": "ak-secret-123456",
        "api": "anthropic-messages", "model_ids": ["claude-sonnet-4-5"], "thinking": "on"})
    assert r.status_code == 200 and "ak-secret-123456" not in r.text
    p = r.json()["providers"][0]
    assert p["api"] == "anthropic-messages" and p["thinking"] == "on"
    # 再存一次不带 thinking：沿用
    r = client.post("/api/settings/models/simple", json={
        "name": "Claude", "base_url": "https://api.anthropic.com", "api": "anthropic-messages"})
    assert r.json()["providers"][0]["thinking"] == "on"
    assert client.post("/api/settings/models/simple", json={
        "name": "x", "base_url": "http://a.test/v1", "api_key": "k", "api": "grpc"}).status_code == 400

    n = net(httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-sonnet-4-1"}]}),
            httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]}))
    d = client.post("/api/settings/models/discover", json={"base_url": "https://api.anthropic.com",
                                                           "api": "anthropic-messages"}).json()
    assert d["probe"] == ["claude-sonnet-4-5"] and d["folded"][0]["id"] == "claude-sonnet-4-1"
    t = client.post("/api/settings/models/test-one", json={"base_url": "https://api.anthropic.com",
                                                           "api": "anthropic-messages", "model": "claude-sonnet-4-5"}).json()
    assert t["ok"] and n.seen[1].headers["x-api-key"] == "ak-secret-123456"      # 用的是存着的密钥
    assert "ak-secret" not in json.dumps(t)
    assert client.post("/api/settings/models/test-one", json={
        "base_url": "https://api.anthropic.com", "model": "bad model; rm -rf"}).status_code == 400
