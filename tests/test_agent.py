"""智能体（「智能体」页）的后端。

模型那头是一个按剧本回话的假网关（httpx.MockTransport）：每次请求按顺序取下一段回答，
并把请求体记下来 —— 要看的正是「模型到底收到了什么」。盯的是几件错了在界面上看不出来的事：

- 只读工具直接跑；会改数据的停下来问；**没批之前数据一个字节都不动**
- 拒绝要告诉模型（不然它以为做成了）；没提到的调用当拒绝
- 「不再询问」只对可撤销的写操作有效，删除每次都问
- 网关不认 tools 参数时退到 JSON 动作协议，并记住
- 轮数上限、停掉之后上下文仍然合法（每个工具调用后面都有一条结果）
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import driver, loop, tools
from app.ai import gateway
from app.ai.openai_compat import OpenAICompatProvider
from app.storage import agent_sessions as sessions
from app.storage import db


# ------------------------------------------------------------------ 假网关
def _sse(chunks: list[dict]) -> bytes:
    out = "".join(f"data: {json.dumps({'choices': [{'delta': c}]}, ensure_ascii=False)}\n\n" for c in chunks)
    return (out + "data: [DONE]\n\n").encode()


def say(text: str) -> bytes:
    """一段普通回答，分两块流出来。"""
    half = len(text) // 2
    return _sse([{"content": text[:half]}, {"content": text[half:]}])


def calls(*items: tuple[str, dict]) -> bytes:
    """几个工具调用。参数故意切成两截发 —— 真网关就是这么一截一截来的。"""
    chunks = []
    for i, (name, args) in enumerate(items):
        a = json.dumps(args, ensure_ascii=False)
        chunks.append({"tool_calls": [{"index": i, "id": f"c{i}_{name}", "type": "function",
                                       "function": {"name": name, "arguments": a[:3]}}]})
        chunks.append({"tool_calls": [{"index": i, "function": {"arguments": a[3:]}}]})
    return _sse(chunks)


class Gateway:
    def __init__(self, *replies, reject_tools: bool = False):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.reject_tools = reject_tools

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.reject_tools and "tools" in body:
            return httpx.Response(400, json={"error": {"message": "tools is not supported by this model"}})
        if not self.replies:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=say("（剧本用完了）"))
        r = self.replies.pop(0)
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=r)


@pytest.fixture()
def gw(workspace, monkeypatch):
    holder = {}

    def install(*replies, **kw):
        g = Gateway(*replies, **kw)
        holder["g"] = g
        monkeypatch.setattr(gateway, "client_factory",
                            lambda timeout=None: httpx.Client(transport=httpx.MockTransport(g)))
        return g

    driver.reset_learned()
    yield install
    driver.reset_learned()


PROVIDER = OpenAICompatProvider("假网关", {"baseUrl": "http://gw.test/v1", "apiKey": "k",
                                         "models": [{"id": "fake"}]})


@pytest.fixture()
def data(workspace):
    """两批、六个样品，两个字段 a / b（b ≈ 2a，有一个偏离的）。"""
    from app.storage import results

    now = db.now()
    ids = {}
    with db.tx() as c:
        for i in range(6):
            sid = f"smp_{i}"
            c.execute("INSERT INTO sample(sample_id, name, batch, created_at) VALUES(?,?,?,?)",
                      (sid, f"S{i}", "B1" if i < 3 else "B2", now))
    for i in range(6):
        run = results.start_run("batch.abs_thickness", "1", sample_id=f"smp_{i}")
        results.write_results(run, [{"field_name": "a", "value": float(i), "unit": "nm", "label": "甲"},
                                    {"field_name": "b", "value": 2.0 * i + (5 if i == 4 else 0), "label": "乙"}],
                              sample_id=f"smp_{i}")
    for r in db.query("SELECT id, sample_id, field_name FROM key_result"):
        ids[(r["sample_id"], r["field_name"])] = r["id"]
    return ids


def run(sid=None, prompt=None, resume=None, permission="ask", mode="auto"):
    sid = sid or sessions.create()
    events = list(loop.run(sid, provider=PROVIDER, model="fake", prompt=prompt, resume=resume,
                           permission=permission, mode=mode))
    return sid, events


def types(events):
    return [e["type"] for e in events if e["type"] not in ("delta", "progress")]


# ------------------------------------------------------------------ 只读
def test_a_plain_answer_needs_no_tools(gw, data):
    g = gw(say("你好，我能帮你查数据。"))
    sid, ev = run(prompt="你好")
    assert types(ev) == ["user", "text", "done"]
    assert ev[-1]["reason"] == "finished"
    assert "".join(e["text"] for e in ev if e["type"] == "delta") == "你好，我能帮你查数据。"
    s = sessions.get(sid)
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"]
    assert s["title"] == "你好"
    # 工具清单真的发给了模型
    assert {t["function"]["name"] for t in g.requests[0]["tools"]} == set(tools.BY_NAME)


def test_read_tools_run_and_their_results_go_back_to_the_model(gw, data):
    g = gw(calls(("list_fields", {}), ("correlate", {"x": "a", "y": "b"})),
           say("甲和乙强相关。"))
    sid, ev = run(prompt="甲和乙有关系吗")
    assert types(ev) == ["user", "tool_call", "tool_call", "tool_result", "tool_result", "text", "done"]
    res = {e["name"]: e for e in ev if e["type"] == "tool_result"}
    assert res["correlate"]["ok"] and res["list_fields"]["ok"]
    out = json.loads(res["correlate"]["detail"])
    assert out["n"] == 6 and out["pearson"]["r"] > 0.9
    assert out["most_off_trend"][0]["name"] == "S4"          # 那个刻意偏离的
    # 第二次请求里有 assistant 的 tool_calls（参数拼完整了）和两条 tool 结果
    msgs = g.requests[1]["messages"]
    tc = next(m for m in msgs if m.get("tool_calls"))["tool_calls"]
    assert json.loads(tc[1]["function"]["arguments"]) == {"x": "a", "y": "b"}
    assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["c0_list_fields", "c1_correlate"]
    assert ev[-1]["turns"] == 2


def test_bad_arguments_come_back_as_a_message_the_model_can_fix(gw, data):
    g = gw(calls(("correlate", {"x": "a"})), calls(("sample_results", {"sample_id": "nope"})), say("好"))
    _, ev = run(prompt="算一下")
    bad = [e for e in ev if e["type"] == "tool_result"]
    assert not bad[0]["ok"] and "缺少必填参数：y" in bad[0]["detail"]
    assert not bad[1]["ok"] and "没有这个样品" in bad[1]["detail"]
    assert any("缺少必填参数" in (m.get("content") or "") for m in g.requests[1]["messages"] if m["role"] == "tool")


# ------------------------------------------------------------------ 会改数据的
def test_a_write_waits_for_approval_and_changes_nothing_until_then(gw, data):
    rid = data[("smp_4", "b")]
    gw(calls(("set_quality", {"result_ids": [rid], "quality": "reject", "note": "偏离"})))
    sid, ev = run(prompt="把 S4 的乙作废")
    assert types(ev)[-2:] == ["approval", "done"] and ev[-1]["reason"] == "awaiting_approval"
    ap = next(e for e in ev if e["type"] == "approval")
    assert "S4 的 乙" in ap["preview"] and "作废" in ap["preview"]
    before = db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,))
    assert before == "review"                              # 没批：一个字节都没动
    assert sessions.get(sid)["pending"]["calls"][0]["name"] == "set_quality"

    g = gw(say("已作废。"))
    _, ev2 = run(sid, resume={"decisions": {ap["id"]: "approve"}})
    assert types(ev2) == ["decision", "tool_result", "text", "done"]
    assert db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,)) == "reject" != before
    assert db.scalar("SELECT quality_note FROM key_result WHERE id=?", (rid,)) == "偏离"
    assert sessions.get(sid)["pending"] is None
    # 模型看得到执行结果
    assert any(m["role"] == "tool" and '"updated": 1' in m["content"] for m in g.requests[0]["messages"])


def test_a_rejection_is_told_to_the_model_and_nothing_changes(gw, data):
    rid = data[("smp_1", "a")]
    gw(calls(("set_quality", {"result_ids": [rid], "quality": "reject"})))
    sid, ev = run(prompt="作废")
    before = db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,))
    g = gw(say("好的，没动。"))
    _, ev2 = run(sid, resume={"decisions": {}})          # 没提到 = 拒绝
    assert ev2[0] == {**ev2[0], "type": "decision", "decision": "rejected"}
    assert db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,)) == before
    tool_msg = next(m for m in g.requests[0]["messages"] if m["role"] == "tool")
    assert "没有执行" in tool_msg["content"]


def test_remember_works_for_writes_but_never_for_deletes(gw, data):
    from app.storage import sets

    st = sets.create("要删的", "pinned", sample_ids=["smp_0"])
    r0, r1 = data[("smp_0", "a")], data[("smp_1", "a")]
    gw(calls(("set_quality", {"result_ids": [r0], "quality": "validated"})))
    sid, ev = run(prompt="标可信")
    ap = next(e for e in ev if e["type"] == "approval")
    gw(say("好"))
    run(sid, resume={"decisions": {ap["id"]: "approve"}, "remember": ["set_quality", "delete_sample_set"]})
    assert sessions.get(sid)["remembered"] == ["set_quality"]          # 删除记不住

    # 同一个会话里再审：不问了，直接做，标着 remembered
    gw(calls(("set_quality", {"result_ids": [r1], "quality": "validated"})), say("好"))
    _, ev = run(sid, prompt="这个也标可信")
    d = next(e for e in ev if e["type"] == "decision")
    assert d["decision"] == "approved" and d.get("remembered")
    assert db.scalar("SELECT quality FROM key_result WHERE id=?", (r1,)) == "validated"
    # 删除仍然要问
    gw(calls(("delete_sample_set", {"set_id": st["set_id"]})))
    _, ev = run(sid, prompt="删掉那个集")
    ap = next(e for e in ev if e["type"] == "approval")
    assert ap["risk"] == "irreversible" and "要删的" in ap["preview"]
    assert sets.get(st["set_id"])                      # 还在


def test_auto_permission_runs_writes_but_still_asks_for_deletes(gw, data):
    from app.storage import sets

    st = sets.create("集", "pinned", sample_ids=["smp_0"])
    rid = data[("smp_2", "a")]
    gw(calls(("set_quality", {"result_ids": [rid], "quality": "reject"}),
             ("delete_sample_set", {"set_id": st["set_id"]})))
    sid, ev = run(prompt="作废并删集", permission="auto")
    assert db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,)) == "reject"
    assert [e["name"] for e in ev if e["type"] == "approval"] == ["delete_sample_set"]
    assert sets.get(st["set_id"])


def test_a_new_message_while_waiting_drops_the_unapproved_call(gw, data):
    rid = data[("smp_3", "a")]
    gw(calls(("set_quality", {"result_ids": [rid], "quality": "reject"})))
    sid, _ = run(prompt="作废")
    before = db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,))
    g = gw(say("好，换个话题。"))
    _, ev = run(sid, prompt="算了，有几个样品？")
    assert ev[0]["type"] == "decision" and ev[0].get("stale")
    assert db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,)) == before
    # 发给模型的上下文仍然合法：每个 tool_call 后面都有它的结果
    msgs = g.requests[0]["messages"]
    asked = {c["id"] for m in msgs for c in m.get("tool_calls") or []}
    answered = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
    assert asked == answered


# ------------------------------------------------------------------ 网关的脾气、闸门
def test_falls_back_to_the_action_protocol_when_tools_are_rejected(gw, data):
    action = json.dumps({"say": "先看字段", "calls": [{"name": "list_fields", "args": {}}]}, ensure_ascii=False)
    g = gw(say(action), say('{"say": "有 a 和 b 两个字段。", "calls": []}'), reject_tools=True)
    _, ev = run(prompt="有哪些字段")
    assert [e["name"] for e in ev if e["type"] == "tool_result"] == ["list_fields"]
    assert ev[-1]["reason"] == "finished" and ev[-1]["driver"] == "action"
    assert next(e for e in reversed(ev) if e["type"] == "text")["text"] == "有 a 和 b 两个字段。"
    # 第一次带 tools 被拒，之后不带；工具结果变成了一条 user 消息
    assert ["tools" in r for r in g.requests] == [True, False, False]
    assert "输出格式" in g.requests[1]["messages"][0]["content"]
    assert any(m["role"] == "user" and "工具结果 list_fields" in m["content"] for m in g.requests[2]["messages"])
    # 记住了：下一句直接走动作协议
    g2 = gw(say('{"say": "好", "calls": []}'), reject_tools=True)
    run(prompt="再问一句")
    assert ["tools" in r for r in g2.requests] == [False]


def test_turn_limit_stops_honestly(gw, data, monkeypatch):
    monkeypatch.setattr(loop, "MAX_TURNS", 3)
    gw(*[calls(("list_fields", {}))] * 5)
    _, ev = run(prompt="一直查")
    assert ev[-1]["reason"] == "max_turns" and ev[-1]["turns"] == 3
    # 三轮都只调工具、没说话：如实说没收敛，不编一个答案
    assert "查了 3 轮还没收敛" in next(e for e in reversed(ev) if e["type"] == "text")["text"]


def test_stopping_midway_still_leaves_a_valid_context(gw, data):
    gw(calls(("list_fields", {}), ("overview", {})))
    sid = sessions.create()
    gen = loop.run(sid, provider=PROVIDER, model="fake", prompt="查")
    for e in gen:
        if e["type"] == "tool_call":
            break                                          # 两个调用都报出来之前就停
    gen.close()
    s = sessions.get(sid)
    assert s["events"][-1]["type"] == "done" and s["events"][-1]["reason"] == "canceled"
    asked = {c["id"] for m in s["messages"] for c in m.get("calls") or []}
    answered = {m["call_id"] for m in s["messages"] if m["role"] == "tool"}
    assert asked and asked == answered


def test_gateway_errors_end_the_turn_with_a_readable_message(gw, data):
    gw(httpx.Response(500, text="upstream exploded"))
    sid, ev = run(prompt="你好")
    assert ev[-2]["type"] == "error" and "500" in ev[-2]["message"]
    assert ev[-1]["reason"] == "failed"


def test_long_contexts_fold_early_tool_results():
    msgs = [{"role": "user", "content": "q"}]
    for i in range(20):
        msgs.append({"role": "assistant", "content": "", "calls": [{"id": f"c{i}", "name": "list_fields", "args": {}}]})
        msgs.append({"role": "tool", "call_id": f"c{i}", "name": "list_fields", "content": "x" * 5000})
    n = loop.compact(msgs)
    assert n > 0 and loop._size(msgs) <= loop.COMPACT_AT_CHARS
    assert msgs[-1]["content"] == "x" * 5000                 # 最近的原样


def test_tool_results_are_clipped_and_say_so(workspace, monkeypatch):
    monkeypatch.setattr(tools, "TOOL_MAX_CHARS", 50)
    r = tools.dispatch("overview", {})
    assert r["ok"] and r["content"].endswith("没看到的不要当作看到了）")
    assert len(r["content"]) < 50 + 60


def test_validation_catches_what_models_get_wrong(workspace):
    t = tools.BY_NAME["set_quality"]
    assert tools.validate(t, {"result_ids": ["3", 4.0], "quality": "reject"})["result_ids"] == [3, 4]
    for bad, msg in (({"quality": "reject"}, "缺少必填参数"),
                     ({"result_ids": [1], "quality": "bad"}, "只能是"),
                     ({"result_ids": [1], "quality": "reject", "x": 1}, "不认识的参数")):
        with pytest.raises(ValueError, match=msg):
            tools.validate(t, bad)


def test_action_parsing_is_forgiving():
    s = driver.parse_action('好的：```json\n{"say": "查一下", "calls": [{"name": "overview"}]}\n```')
    assert s.text == "查一下" and [c.name for c in s.calls] == ["overview"]
    s = driver.parse_action("就是一段普通的话，没有 JSON")
    assert s.text == "就是一段普通的话，没有 JSON" and not s.calls


# ------------------------------------------------------------------ 接口
@pytest.fixture()
def client(workspace):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _configure_model():
    from app.ai import openai_compat
    openai_compat.save_config({"providers": {"假网关": {
        "baseUrl": "http://gw.test/v1", "apiKey": "k", "api": "openai-completions", "models": [{"id": "fake"}]}}})


def _frames(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_api_no_model_is_501_before_the_stream(client):
    r = client.post("/api/agent/run", json={"prompt": "你好"})
    assert r.status_code == 501
    assert sessions.list_all() == []                 # 没配模型就不建会话


def test_api_run_resume_and_sessions(client, gw, data):
    _configure_model()
    rid = data[("smp_5", "b")]
    gw(calls(("set_quality", {"result_ids": [rid], "quality": "reject"})), say("已作废"))
    fr = _frames(client.post("/api/agent/run", json={"prompt": "作废 S5 的乙"}).text)
    assert fr[0][0] == "session"
    sid = fr[0][1]["session_id"]
    ap = next(d for e, d in fr if e == "approval")
    got = client.get(f"/api/agent/sessions/{sid}").json()
    assert got["pending"][0]["id"] == ap["id"] and got["title"] == "作废 S5 的乙"
    assert client.get("/api/agent/sessions").json()["sessions"][0]["waiting"] is True

    fr2 = _frames(client.post(f"/api/agent/sessions/{sid}/resume",
                              json={"decisions": {ap["id"]: "approve"}}).text)
    assert [e for e, _ in fr2 if e not in ("delta", "progress")] == ["session", "decision", "tool_result", "text", "done"]
    assert db.scalar("SELECT quality FROM key_result WHERE id=?", (rid,)) == "reject"
    assert client.post(f"/api/agent/sessions/{sid}/resume", json={"decisions": {}}).status_code == 409
    assert client.delete(f"/api/agent/sessions/{sid}").json()["ok"]
    assert client.get(f"/api/agent/sessions/{sid}").status_code == 404


def test_api_tools_and_settings(client):
    t = client.get("/api/agent/tools").json()
    risks = {x["name"]: x["risk"] for x in t["tools"]}
    assert risks["set_quality"] == "write" and risks["delete_sample_set"] == "irreversible"
    assert sum(r == "read" for r in risks.values()) == 9 and t["limits"]["max_turns"] == loop.MAX_TURNS
    assert client.get("/api/agent/settings").json() == {"permission": "ask", "driver": "auto"}
    assert client.post("/api/agent/settings", json={"permission": "auto"}).json()["permission"] == "auto"
    assert client.post("/api/agent/settings", json={"permission": "yolo"}).status_code == 400
