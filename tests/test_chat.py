"""AI 抽屉的后端。

这里盯的是几件「错了在界面上看不出来」的事：
- 没配模型时 501 必须在**开流之前**发（开了流就再也改不了状态码）
- 范围存的是筛选式，不是样品 ID 列表
- 原始光谱矩阵永远不进 prompt
- 删会话要真的级联删消息
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.api import chat as chat_api
from app.storage import conversations, db


@pytest.fixture()
def client(workspace):
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def samples(workspace):
    """几个样品 + 关键结果，够上下文装配用。"""
    ts = db.now()
    with db.tx() as conn:
        for i in range(6):
            sid = f"s{i}"
            conn.execute(
                "INSERT INTO sample (sample_id, name, batch, created_at) VALUES (?,?,?,?)",
                (sid, f"ZG00{13 + i}_20260729183547{i:02d}_Mode5", f"ZG00{13 + i}", ts))
            conn.execute(
                "INSERT INTO measurement (measurement_id, sample_id, method, measured_at,"
                "                         created_at) VALUES (?,?,?,?,?)",
                (f"m{i}", sid, "absorbance", f"2026-07-29T18:3{i}:47", ts))
            conn.execute(
                "INSERT INTO key_result (sample_id, field_name, value_num, unit,"
                "                        source, created_at) VALUES (?,?,?,?,?,?)",
                (sid, "integral_final", 100.0 + i, "a.u.·nm", "skill", ts))
    return 6


# ---------------------------------------------------------------- 落库
def test_conversation_and_messages_persist(workspace):
    cid = conversations.create("试一下", {"mode": "all", "filter": {}})
    conversations.add_message(cid, "user", "这批膜厚怎么样？")
    conversations.add_message(cid, "assistant", "大部分在 DEGRADED 档。")

    conv = conversations.get(cid)
    assert conv["title"] == "试一下"
    msgs = conversations.messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "这批膜厚怎么样？"


def test_deleting_a_conversation_cascades_to_its_messages(workspace):
    """外键级联真的要生效。

    db.connect 里那句 `PRAGMA foreign_keys = ON` 是这条测试的全部前提 ——
    SQLite 默认是关的，没有那句的话删完会静默留下一堆孤儿消息，
    只有在磁盘涨起来的时候才会发现。
    """
    cid = conversations.create()
    conversations.add_message(cid, "user", "一句话")
    other = conversations.create()
    conversations.add_message(other, "user", "另一个会话的话")

    assert conversations.delete(cid) is True
    assert db.scalar("SELECT COUNT(*) FROM message WHERE conversation_id=?", (cid,)) == 0
    # 只删该删的那个
    assert db.scalar("SELECT COUNT(*) FROM message WHERE conversation_id=?", (other,)) == 1


def test_empty_assistant_messages_are_left_out_of_model_history(workspace):
    """上一轮被中途停掉、一个字没出来的助手消息不该喂回给模型。"""
    cid = conversations.create()
    conversations.add_message(cid, "user", "问题一")
    conversations.add_message(cid, "assistant", "")        # 被停掉的那条
    conversations.add_message(cid, "user", "问题二")
    hist = conversations.history_for_model(cid)
    assert [m["content"] for m in hist] == ["问题一", "问题二"]


# ---------------------------------------------------------------- 钉住
def test_pinned_note_survives_the_conversation_being_deleted(workspace):
    """钉住的是正文快照，不是外键。

    用户钉它就是因为它值得留下 —— 清理对话历史不该顺手把对比页上的分析抹掉。
    """
    cid = conversations.create()
    mid = conversations.add_message(cid, "assistant", "这批的积分比偏低。")
    conversations.pin("run_abc", cid, mid, "这批的积分比偏低。")

    conversations.delete(cid)
    pins = conversations.pins_for("run_abc")
    assert len(pins) == 1
    assert pins[0]["note"] == "这批的积分比偏低。"
    assert conversations.pin_counts() == {"run_abc": 1}


# ---------------------------------------------------------------- 上下文装配
def test_small_scope_gets_per_sample_detail(samples):
    from app.ai import context

    built = context.build({"mode": "all", "filter": {}})
    assert built["n_samples"] == 6
    assert built["needs_narrowing"] is False
    detail = built["facts"]["样品明细"]
    assert len(detail) == 6
    assert detail[0]["关键结果"]["integral_final(a.u.·nm)"] == 100.0


def test_large_scope_gets_overview_only_and_says_why(samples):
    """超过上限时不给逐条明细，而且要**说出来**。

    静默截断的话，模型会对着半个列表下全局结论，而且没人知道它看漏了。
    """
    from app.ai import context

    built = context.build({"mode": "all", "filter": {}}, detail_max=3)
    assert built["needs_narrowing"] is True
    assert "样品明细" not in built["facts"]
    why = built["facts"]["为什么没有逐个样品的明细"]
    assert "6" in why and "3" in why


def test_empty_scope_says_so_instead_of_looking_normal(workspace):
    from app.ai import context

    built = context.build({"mode": "all", "filter": {}})
    assert built["n_samples"] == 0
    assert "一个样品都没有" in built["facts"]["提示"]


def test_raw_matrix_never_reaches_the_prompt(samples, tmp_path):
    """★ 一个样品 200 帧 × 2000 波长就能把本地 27B 的窗口撑爆。

    这条测试的价值不在今天 —— 今天的 _samples 只取标量。它防的是以后
    有人图省事往事实包里塞一段矩阵。
    """
    from app.ai import context

    built = context.build({"mode": "all", "filter": {}})
    text = context.to_prompt(built["facts"])
    assert len(text.encode("utf-8")) < 8000
    # 数组套数组是矩阵的形状特征
    assert "[[" not in text


def test_oversized_facts_are_truncated_loudly(workspace):
    from app.ai import context

    facts = {"一堆东西": ["x" * 100 for _ in range(2000)]}
    text = context.to_prompt(facts, byte_limit=2000)
    assert "已截断" in text
    assert "请先收窄范围再下结论" in text


# ---------------------------------------------------------------- API
def test_scope_preview_is_a_plain_db_query(client, samples):
    """切换「选中的 / 全部的」不该等模型。"""
    r = client.post("/api/chat/scope/preview", json={"scope": {"mode": "all", "filter": {}}})
    assert r.status_code == 200
    assert r.json()["n_samples"] == 6
    assert r.json()["detail_max"] == 40


def test_scope_stores_a_filter_expression_not_an_id_list(client, samples):
    """★ 范围存筛选式。

    存 ID 快照的话，过两天点开历史看到的是一批可能已经不存在的样品 ——
    同一类「存快照还是存规则」的 bug 在这个项目里已经栽过两次。
    """
    r = client.post("/api/chat/conversations",
                    json={"scope": {"mode": "selected", "filter": {"batch": ["ZG0013"]}}})
    cid = r.json()["conversation"]["conversation_id"]

    raw = db.scalar("SELECT scope_json FROM conversation WHERE conversation_id=?", (cid,))
    stored = json.loads(raw)
    assert stored["filter"] == {"batch": ["ZG0013"]}
    assert "ids" not in stored
    assert "sample_ids" not in json.dumps(stored)


def test_bad_filter_in_scope_is_rejected_not_ignored(client, samples):
    """砍掉的键要拒绝，不能静默忽略 —— 静默忽略等于范围悄悄变成了全库。"""
    r = client.post("/api/chat/conversations",
                    json={"scope": {"mode": "all", "filter": {"没这个键": [1]}}})
    assert r.status_code == 400


def test_sending_without_a_model_fails_before_the_stream_opens(client, samples):
    """★ 501 必须是个正常的 JSON 响应。

    一旦 StreamingResponse 开始产出，状态码就已经在路上了，
    main.py 那个全局异常处理器再也插不上手 —— 用户看到的会是
    「200 OK + 半截空响应」。所以没配模型这类错必须同步抛。
    """
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]
    r = client.post(f"/api/chat/conversations/{cid}/messages",
                    json={"content": "这批怎么样？"})
    assert r.status_code == 501
    assert r.json()["error"]["kind"] == "no_model"
    assert "设置" in r.json()["error"]["message"] or "providers" in r.json()["error"]["message"]
    # 而且不能是 text/event-stream
    assert "event-stream" not in r.headers.get("content-type", "")


def test_empty_question_is_rejected(client):
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]
    assert client.post(f"/api/chat/conversations/{cid}/messages",
                       json={"content": "   "}).status_code == 400


def test_missing_conversation_is_404_not_500(client):
    assert client.get("/api/chat/conversations/conv_nope").status_code == 404
    assert client.delete("/api/chat/conversations/conv_nope").status_code == 404


def test_pin_roundtrip_through_the_api(client):
    r = client.post("/api/chat/pins",
                    json={"analysis_run_id": "run_1", "note": "积分比偏低"})
    assert r.status_code == 200
    pid = r.json()["pin_id"]
    assert client.get("/api/chat/pins", params={"run": "run_1"}).json()["pins"][0]["note"] \
        == "积分比偏低"
    assert client.get("/api/chat/pins").json()["counts"] == {"run_1": 1}
    assert client.delete(f"/api/chat/pins/{pid}").status_code == 200
    assert client.get("/api/chat/pins").json()["counts"] == {}


def test_pinning_nothing_is_refused(client):
    assert client.post("/api/chat/pins",
                       json={"analysis_run_id": "run_1", "note": "  "}).status_code == 400


# ---------------------------------------------------------------- SSE 切帧
def test_sse_frames_survive_the_shapes_gateways_actually_send():
    """网关的实现千奇百怪，这几种都得扛住。

    半截 json 那条是重点：崩在那里的话用户看到的是**整个回答消失**，
    而不是少几个字。
    """
    from app.ai.openai_compat import _iter_sse_text

    lines = [
        ": ping",                                            # 心跳注释
        "",                                                  # 空行分隔
        'data: {"choices":[{"delta":{"content":"膜厚"}}]}',   # data: 后有空格
        'data:{"choices":[{"delta":{"content":"偏薄"}}]}',    # 没空格
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',  # 只有角色，没正文
        "data: {半截 json",                                   # 坏帧，丢这一块就行
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"不该出现"}}]}',
    ]
    assert list(_iter_sse_text(lines)) == ["膜厚", "偏薄"]


# ---------------------------------------------------------------- 端到端流式
class _FakeProvider:
    """假 provider。不打网络，按脚本吐字或中途抛。"""

    name = "fake"

    def __init__(self, pieces, boom=None):
        self.pieces, self.boom = pieces, boom
        self.seen = None

    def available(self):
        return True

    def chat_stream(self, messages, **kw):
        self.seen = messages
        for i, p in enumerate(self.pieces):
            if self.boom is not None and i == self.boom:
                from app.ai.provider import ProviderUnavailable
                raise ProviderUnavailable("网关 502")
            yield p


def _use(monkeypatch, provider):
    from app.api import chat as chat_api
    monkeypatch.setattr(chat_api, "_resolve_or_501",
                        lambda *a, **k: (provider, "fake-model"))
    return provider


def _frames(text):
    """把 SSE 响应体拆成 [(event, data), ...]。"""
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            out.append((ev, data))
    return out


def test_streaming_answer_arrives_in_pieces_and_lands_in_the_db(client, samples, monkeypatch):
    _use(monkeypatch, _FakeProvider(["这批", "膜厚", "偏薄。"]))
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]

    r = client.post(f"/api/chat/conversations/{cid}/messages",
                    json={"content": "这批膜厚怎么样？"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]

    frames = _frames(r.text)
    kinds = [k for k, _ in frames]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert [d["text"] for k, d in frames if k == "delta"] == ["这批", "膜厚", "偏薄。"]

    msgs = conversations.messages(cid)
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "这批膜厚偏薄。"     # 攒起来的正文写回去了
    assert msgs[0]["content"] == "这批膜厚怎么样？"    # 用户那条先落的库


def test_the_model_sees_the_facts_not_the_raw_data(client, samples, monkeypatch):
    p = _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations",
                      json={"scope": {"mode": "all", "filter": {}}}).json()[
        "conversation"]["conversation_id"]
    client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "几个样品？"})

    blob = "\n".join(str(m.content) for m in p.seen)
    assert "已知事实" in blob
    assert "命中样品数" in blob
    assert "[[" not in blob                     # 矩阵的形状特征，一次都不该出现


def test_a_mid_stream_failure_becomes_an_error_frame_not_a_dead_connection(
        client, samples, monkeypatch):
    """★ 流开了之后再出错，只能靠 error 帧告诉用户。

    抛给框架的话响应头早发出去了，用户看到的是连接莫名其妙断掉 ——
    这正是 main.py 那个全局异常处理器管不到的地方。
    """
    _use(monkeypatch, _FakeProvider(["答了一半", "就炸了"], boom=1))
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]

    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "问"})
    assert r.status_code == 200                 # 头早就发出去了，改不了
    frames = _frames(r.text)
    assert ("error" in [k for k, _ in frames])
    err = [d for k, d in frames if k == "error"][0]
    assert "502" in err["message"]
    assert err["partial"] is True

    # 半截答案也要留下，别让用户以为什么都没发生
    msgs = conversations.messages(cid)
    assert msgs[-1]["content"] == "答了一半"
    assert msgs[-1]["meta"]["error"]


def test_first_question_becomes_the_conversation_title(client, samples, monkeypatch):
    _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]
    client.post(f"/api/chat/conversations/{cid}/messages",
                json={"content": "ZG0014 那两次测量有什么区别？"})
    assert conversations.get(cid)["title"].startswith("ZG0014")


# ---------------------------------------------------------------- 动作卡片
def test_a_select_card_is_parsed_out_of_the_answer(client, samples, monkeypatch):
    _use(monkeypatch, _FakeProvider([
        "帮你筛出 ZG0013 的。\n\n```json\n",
        '{"action":"select","filter":{"batch":["ZG0013"]},"why":"你问的是这一批"}',
        "\n```"]))
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]
    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "选 ZG0013"})

    cards = [d for k, d in _frames(r.text) if k == "card"]
    assert len(cards) == 1
    assert cards[0]["action"] == "select"
    assert cards[0]["filter"] == {"batch": ["ZG0013"]}
    assert cards[0]["count"] == 1              # 卡片上直接写清楚会命中几个


def test_a_card_that_hands_back_id_lists_gets_them_stripped(client, samples, monkeypatch):
    """★ 白名单要在代码里，不能只写在提示词里。

    提示词是建议，代码才是保证。模型给 ID 列表时错了你看到的是错的结果；
    给筛选式时错了你看到的是错的 chip —— 后者能改，前者不能。
    """
    _use(monkeypatch, _FakeProvider([
        '```json\n{"action":"select","filter":{"ids":["s0","s1"],"batch":["ZG0013"]}}\n```']))
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]
    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "选几个"})

    card = [d for k, d in _frames(r.text) if k == "card"][0]
    assert "ids" not in card["filter"]
    assert card["filter"] == {"batch": ["ZG0013"]}


def test_an_invented_action_is_ignored(client, samples, monkeypatch):
    """模型编一个白名单外的动作出来时，界面应该当它没说。"""
    _use(monkeypatch, _FakeProvider(['```json\n{"action":"delete_everything"}\n```']))
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]
    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "删了吧"})
    assert not [d for k, d in _frames(r.text) if k == "card"]


def test_a_plain_answer_produces_no_card(client, samples, monkeypatch):
    _use(monkeypatch, _FakeProvider(["这批的积分比在 0.7 上下，属于正常范围。"]))
    cid = client.post("/api/chat/conversations", json={}).json()[
        "conversation"]["conversation_id"]
    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "怎么样"})
    assert not [d for k, d in _frames(r.text) if k == "card"]


# ---------------------------------------------------------------- 术语 ⓘ 弹窗
# 界面上每个词旁边那个小圆圈点开是一条独立的对话线。三件事要钉住：
# 线之间不串、定义原文真的进了 prompt、别拿一张 40 行的样品表去答一个名词解释。

def test_each_term_gets_its_own_thread(client, samples, monkeypatch):
    _use(monkeypatch, _FakeProvider(["嗯"]))
    a = client.post("/api/chat/conversations",
                    json={"scope": {"topic": "glossary:ot"}}).json()
    b = client.post("/api/chat/conversations",
                    json={"scope": {"topic": "glossary:cycles"}}).json()
    client.post("/api/chat/conversations/"
                f"{a['conversation']['conversation_id']}/messages",
                json={"content": "OT 是什么"})

    only_ot = client.get("/api/chat/conversations", params={"topic": "glossary:ot"}).json()
    assert [c["conversation_id"] for c in only_ot["conversations"]] \
        == [a["conversation"]["conversation_id"]]

    # 不带 topic 还是全都列出来 —— 抽屉的历史列表里也找得到这些
    every = client.get("/api/chat/conversations").json()["conversations"]
    ids = {c["conversation_id"] for c in every}
    assert a["conversation"]["conversation_id"] in ids
    assert b["conversation"]["conversation_id"] in ids


def test_the_definition_on_screen_is_what_the_model_is_told(client, samples, monkeypatch):
    """术语表是界面文案，后端不另存一份 —— 但送进 prompt 的必须是同一份。"""
    p = _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations",
                      json={"scope": {"topic": "glossary:ot_status"}}).json()[
        "conversation"]["conversation_id"]
    client.post(f"/api/chat/conversations/{cid}/messages",
                json={"content": "为什么这帧不可信？",
                      "context_note": "可信度判级\n判据只打标志，绝不修改数值。"})

    system = p.seen[0].content
    assert "判据只打标志，绝不修改数值。" in system
    assert "不要输出 json 动作块" in system    # 名词解释不该诱它去操作平台


def test_a_term_question_does_not_drag_in_the_sample_table(client, samples, monkeypatch):
    """问「LOW_CYCLES 是什么意思」时，40 行样品明细帮不上忙，
    「请先给一个收窄的筛选式」那句更是彻底跑偏。"""
    p = _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations",
                      json={"scope": {"topic": "glossary:cycles"}}).json()[
        "conversation"]["conversation_id"]
    client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "条纹数是什么"})

    blob = "\n".join(str(m.content) for m in p.seen)
    assert "样品明细" not in blob
    assert "收窄" not in blob
    assert "概览" in blob                       # L1 汇总还是给的，好把定义落到他的数据上


def test_topic_is_a_label_not_a_place_to_stash_sample_ids(client, samples):
    """topic 只是一根线的名字。范围仍然只认筛选式 —— 别让它变成第二个后门。"""
    r = client.post("/api/chat/conversations",
                    json={"scope": {"topic": "glossary:ot", "ids": ["s1", "s2"]}})
    scope = r.json()["conversation"]["scope"]
    assert scope["topic"] == "glossary:ot"
    assert "ids" not in scope
    assert scope["filter"] == {}


# ------------------------------------------------------------------ 眼前这一处
#
# 「点开 ⓘ 之后模型到底知道什么」这条链路。以前它只知道术语表那段定义 ——
# 你点开 18–19 s 那个「一帧可信的都没有」问「为什么」，它只能把定义换个说法重讲。
def test_the_numbers_on_screen_reach_the_model(client, samples, monkeypatch):
    """界面把眼前这一处的读数拼进 context_note，必须原样进 prompt。"""
    p = _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations",
                      json={"scope": {"topic": "glossary:ot_status"}}).json()[
        "conversation"]["conversation_id"]
    note = ("可信度判级\n每一帧分三档。\n\n"
            "「不同时刻的平均膜厚」里 18–19 s 这个窗口\n"
            "  ZG0013_…1932　2204 nm　0/10 帧可信\n"
            "  ZG0014_…1833　1732 nm　0/10 帧可信")
    client.post(f"/api/chat/conversations/{cid}/messages",
                json={"content": "为什么这一秒全都不可信？", "context_note": note})

    system = p.seen[0].content
    assert "2204 nm" in system and "0/10 帧可信" in system
    assert "18–19 s 这个窗口" in system
    # prompt 得告诉模型这一段是什么、以及不许编数
    assert "眼前这一处" in system
    assert "不要编" in system


def test_a_long_note_is_capped_but_not_at_the_old_2000(client, samples, monkeypatch):
    """读数比定义长得多。上限还在（不能让界面往 prompt 里灌任意长的东西），
    但不能卡在只够放定义的那个数上。"""
    p = _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations",
                      json={"scope": {"topic": "glossary:ot_status"}}).json()[
        "conversation"]["conversation_id"]
    # 用一个 prompt 里不会出现的字符当标记 —— 「数」在 GLOSSARY_PROMPT 里本来就有几个
    client.post(f"/api/chat/conversations/{cid}/messages",
                json={"content": "?", "context_note": "囧" * 9000})

    system = p.seen[0].content
    assert system.count("囧") == chat_api.CONTEXT_NOTE_MAX
    assert chat_api.CONTEXT_NOTE_MAX > 2000


def test_a_term_question_about_a_named_sample_does_get_its_row(client, samples,
                                                               monkeypatch):
    """ⓘ 说清了在看哪个样品时，那一行明细恰恰就是问题本身。

    「术语对话不给样品明细」这条规矩的理由是「40 行帮不上忙」——
    范围窄到一个样品时它不成立。
    """
    p = _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations", json={
        "scope": {"topic": "glossary:ot_status", "filter": {"ids": ["s1"]}}}).json()[
        "conversation"]["conversation_id"]
    client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "这个样品呢"})

    blob = "\n".join(str(m.content) for m in p.seen)
    assert "样品明细" in blob
    # 但仍然只有那一个样品，不是整库倒出来
    assert blob.count("ZG00") < 6


def test_a_follow_up_still_knows_which_spot_it_was_about(client, samples, monkeypatch):
    """「为什么？」这种追问不能掉上下文。

    读数只在 system 里给过一次、不进正文 —— 不把它拼回历史的话，
    第二问到模型那儿是一句光秃秃的「为什么」，它不知道是哪一秒。
    """
    p = _use(monkeypatch, _FakeProvider(["因为条纹不够", "再说一遍"]))
    cid = client.post("/api/chat/conversations",
                      json={"scope": {"topic": "glossary:ot_status"}}).json()[
        "conversation"]["conversation_id"]
    client.post(f"/api/chat/conversations/{cid}/messages",
                json={"content": "为什么全不可信", "spot": "18–19 s 这个窗口 · 3 项读数"})
    client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "为什么？"})

    replay = "\n".join(str(m.content) for m in p.seen)
    assert "18–19 s 这个窗口" in replay


def test_the_run_id_picks_that_comparison_not_the_latest(client, samples, monkeypatch):
    """在对比页上点开的 ⓘ 问的是**眼前这一次**，而「最近一次」未必是它
    —— 你可能正翻着上周那次的结果。"""
    with db.tx() as conn:
        for rid, title, when in (("run_aaa111", "老的那次", "2026-01-01T00:00:00"),
                                 ("run_bbb222", "新的那次", "2026-09-01T00:00:00")):
            conn.execute(
                "INSERT INTO analysis_run (analysis_run_id, skill_id, skill_version,"
                "                          status, params_json, started_at)"
                " VALUES (?,?,?,?,?,?)",
                (rid, "batch.abs_thickness", "0.2.0", "ok",
                 json.dumps({"title": title}), when))

    p = _use(monkeypatch, _FakeProvider(["好"]))
    cid = client.post("/api/chat/conversations", json={
        "scope": {"topic": "glossary:ot", "run_id": "run_aaa111"}}).json()[
        "conversation"]["conversation_id"]
    client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "这次怎么样"})

    blob = "\n".join(str(m.content) for m in p.seen)
    assert "老的那次" in blob and "新的那次" not in blob
    assert "你正在看的这次对比" in blob


def test_run_id_must_look_like_a_run_id(client, samples):
    """scope 里只放**能重新解析的 id**。乱写的要当场拒掉，
    不能让它变成往范围里塞任意字符串的后门。"""
    ok = client.post("/api/chat/conversations",
                     json={"scope": {"topic": "glossary:ot", "run_id": "run_1ebd03b4c6f4"}})
    assert ok.json()["conversation"]["scope"]["run_id"] == "run_1ebd03b4c6f4"

    for bad in ("../../etc/passwd", "run_", "DROP TABLE", "smp_8bd0030642e6"):
        r = client.post("/api/chat/conversations",
                        json={"scope": {"topic": "glossary:ot", "run_id": bad}})
        assert r.status_code == 400, bad
