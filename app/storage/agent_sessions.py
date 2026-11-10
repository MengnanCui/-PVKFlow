"""智能体会话：上下文、界面事件、停在审批上的那一轮，都存在 agent_session 的一行里。

为什么审批的状态存服务端、不让浏览器拿着回传（和 IFlow 的 lib/agent/pending.ts 同一个理由）：
那样整段上下文（含工具读回来的数）要在网络上来回搬；更要紧的是，**恢复出来的上下文
就不再是服务端算过的那一份**，出了问题说不清到底喂给模型的是什么。审批只是中间停一下。
"""
from __future__ import annotations

import json
from typing import Any

from app.storage import db
from app.errors import NotFound

DEFAULT_TITLE = "新任务"


def _load(row: dict | None) -> dict | None:
    if not row:
        return None
    out = dict(row)
    out["messages"] = json.loads(out.pop("messages_json") or "[]")
    out["events"] = json.loads(out.pop("events_json") or "[]")
    pend = out.pop("pending_json")
    out["pending"] = json.loads(pend) if pend else None
    out["remembered"] = json.loads(out.pop("remembered_json") or "[]")
    return out


def create(title: str = DEFAULT_TITLE) -> str:
    sid = db.new_id("agt")
    now = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO agent_session(session_id, title, created_at, updated_at)"
                  " VALUES(?,?,?,?)", (sid, (title or DEFAULT_TITLE)[:60], now, now))
    return sid


def get(session_id: str) -> dict:
    s = _load(db.query_one("SELECT * FROM agent_session WHERE session_id = ?", (session_id,)))
    if not s:
        raise NotFound(f"没有这个会话：{session_id}")
    return s


def list_all(limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT session_id, title, model, created_at, updated_at,"
        "       pending_json IS NOT NULL AS waiting"
        " FROM agent_session ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,))
    for r in rows:
        r["waiting"] = bool(r["waiting"])
    return rows


def save(session_id: str, *, messages: list | None = None, events: list | None = None,
         pending: Any = ..., remembered: list | None = None, model: str | None = None,
         title: str | None = None) -> None:
    """只写给了的那几样。pending 用 ... 表示「不动」，None 表示「清掉」。"""
    sets, params = ["updated_at = ?"], [db.now()]
    if messages is not None:
        sets.append("messages_json = ?")
        params.append(json.dumps(messages, ensure_ascii=False))
    if events is not None:
        sets.append("events_json = ?")
        params.append(json.dumps(events, ensure_ascii=False))
    if pending is not ...:
        sets.append("pending_json = ?")
        params.append(None if pending is None else json.dumps(pending, ensure_ascii=False))
    if remembered is not None:
        sets.append("remembered_json = ?")
        params.append(json.dumps(sorted(set(remembered))))
    if model is not None:
        sets.append("model = ?")
        params.append(model)
    if title is not None:
        sets.append("title = ?")
        params.append(title[:60] or DEFAULT_TITLE)
    with db.tx() as c:
        c.execute(f"UPDATE agent_session SET {', '.join(sets)} WHERE session_id = ?",
                  (*params, session_id))


def delete(session_id: str) -> None:
    get(session_id)
    with db.tx() as c:
        c.execute("DELETE FROM agent_session WHERE session_id = ?", (session_id,))
