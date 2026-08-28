"""AI 会话的落库。

今天 `/api/assist/ask` 是无状态的：现拼两条消息、答完就丢。抽屉要有历史，
就得有个地方存。这里只管存取，不碰模型 —— 拼上下文在 `app/ai/context.py`，
调模型在 `app/ai/openai_compat.py`。

一条原则贯穿全文件：**scope 存筛选式，不存样品 ID 列表**。
样品会增删、筛选式能复算，存快照的话过两天点开历史看到的是一批不存在的 ID。
（同一类「存快照还是存规则」的取舍在 sample_set 那边已经做过一次。）
"""
from __future__ import annotations

import json
from typing import Any

from app.storage import db

# 一次对话最多回溯多少轮喂给模型。再多的话本地 27B 的上下文窗口先撑不住。
HISTORY_TURNS = 12


# ---------------------------------------------------------------- 会话
def create(title: str = "新对话", scope: dict | None = None) -> str:
    cid = db.new_id("conv")
    ts = db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO conversation (conversation_id, title, scope_json,"
            "                          created_at, updated_at) VALUES (?,?,?,?,?)",
            (cid, title or "新对话", json.dumps(scope or {}, ensure_ascii=False), ts, ts))
    return cid


def get(conversation_id: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM conversation WHERE conversation_id = ?", (conversation_id,))
    return _row(row) if row else None


def list_recent(limit: int = 50, topic: str | None = None) -> list[dict]:
    """最近的对话。

    `topic` 是给术语 ⓘ 弹窗用的：每个术语一条独立的对话线，点开只看这一条
    的历史。存在 scope 里而不是 localStorage —— 换台机器、清了浏览器缓存，
    问过的话都还在，而且在抽屉的历史列表里也找得到。
    """
    where = "" if topic is None else " WHERE json_extract(c.scope_json, '$.topic') = ?"
    args: tuple = (limit,) if topic is None else (topic, limit)
    rows = db.query(
        "SELECT c.*, ("
        "   SELECT COUNT(*) FROM message m WHERE m.conversation_id = c.conversation_id"
        " ) AS n_messages"
        " FROM conversation c" + where +
        " ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ?", args)
    return [_row(r) for r in rows]


def rename(conversation_id: str, title: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversation SET title = ?, updated_at = ? WHERE conversation_id = ?",
            (title.strip()[:120] or "新对话", db.now(), conversation_id))


def set_scope(conversation_id: str, scope: dict) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE conversation SET scope_json = ?, updated_at = ? WHERE conversation_id = ?",
            (json.dumps(scope or {}, ensure_ascii=False), db.now(), conversation_id))


def delete(conversation_id: str) -> bool:
    """删会话。消息靠外键 ON DELETE CASCADE 一起走 —— db.connect 里
    已经 `PRAGMA foreign_keys = ON`，没那句的话这里会静默留下一堆孤儿消息。"""
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM conversation WHERE conversation_id = ?", (conversation_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- 消息
def add_message(conversation_id: str, role: str, content: str,
                meta: dict | None = None) -> str:
    mid = db.new_id("msg")
    ts = db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO message (message_id, conversation_id, role, content,"
            "                     meta_json, created_at) VALUES (?,?,?,?,?,?)",
            (mid, conversation_id, role, content,
             json.dumps(meta or {}, ensure_ascii=False), ts))
        conn.execute("UPDATE conversation SET updated_at = ? WHERE conversation_id = ?",
                     (ts, conversation_id))
    return mid


def update_message(message_id: str, content: str, meta: dict | None = None) -> None:
    """流结束（或中断）时把攒下来的正文写回去。"""
    with db.tx() as conn:
        if meta is None:
            conn.execute("UPDATE message SET content = ? WHERE message_id = ?",
                         (content, message_id))
        else:
            conn.execute("UPDATE message SET content = ?, meta_json = ? WHERE message_id = ?",
                         (content, json.dumps(meta, ensure_ascii=False), message_id))


def messages(conversation_id: str, limit: int = 500) -> list[dict]:
    rows = db.query(
        "SELECT * FROM message WHERE conversation_id = ?"
        " ORDER BY created_at ASC, rowid ASC LIMIT ?", (conversation_id, limit))
    return [_msg(r) for r in rows]


def history_for_model(conversation_id: str, turns: int = HISTORY_TURNS) -> list[dict]:
    """喂给模型的历史：只要 user/assistant 的正文，去掉空的助手消息。

    空的助手消息是「上一轮被中途停掉、一个字都没出来」留下的。带上去只会
    让模型以为自己上次答了个空，不如当它没发生过。
    """
    out = []
    for m in messages(conversation_id):
        if m["role"] not in ("user", "assistant"):
            continue
        if m["role"] == "assistant" and not m["content"].strip():
            continue
        out.append({"role": m["role"], "content": m["content"]})
    return out[-turns * 2:] if turns else out


# ---------------------------------------------------------------- 钉住
def pin(analysis_run_id: str, conversation_id: str | None,
        message_id: str | None, note: str) -> str:
    """把一条回答钉到某次对比上。

    `note` 存的是**正文快照**，不是外键取回来的。会话被删掉之后，钉在对比页上
    的那段分析还应该在 —— 用户钉它就是因为它值得留下。
    """
    pid = db.new_id("pin")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO ai_pin (pin_id, analysis_run_id, conversation_id,"
            "                    message_id, note, created_at) VALUES (?,?,?,?,?,?)",
            (pid, analysis_run_id, conversation_id, message_id, note, db.now()))
    return pid


def pins_for(analysis_run_id: str) -> list[dict]:
    rows = db.query(
        "SELECT p.*, c.title AS conversation_title FROM ai_pin p"
        " LEFT JOIN conversation c ON c.conversation_id = p.conversation_id"
        " WHERE p.analysis_run_id = ? ORDER BY p.created_at DESC, p.rowid DESC",
        (analysis_run_id,))
    return [dict(r) for r in rows]


def pin_counts() -> dict[str, int]:
    """对比历史列表要在行尾显示「📌 2」，一次查完，别一行一个查询。"""
    rows = db.query(
        "SELECT analysis_run_id, COUNT(*) AS n FROM ai_pin GROUP BY analysis_run_id")
    return {r["analysis_run_id"]: r["n"] for r in rows}


def unpin(pin_id: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM ai_pin WHERE pin_id = ?", (pin_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- 行转换
def _json(raw: Any) -> dict:
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _row(r: dict) -> dict:
    return {
        "conversation_id": r["conversation_id"],
        "title": r["title"],
        "scope": _json(r["scope_json"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "n_messages": r.get("n_messages"),
    }


def _msg(r: dict) -> dict:
    return {
        "message_id": r["message_id"],
        "conversation_id": r["conversation_id"],
        "role": r["role"],
        "content": r["content"],
        "meta": _json(r["meta_json"]),
        "created_at": r["created_at"],
    }
