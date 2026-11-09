"""智能体的接口：发一句话 / 批完接着跑（都是 SSE 流）、会话列表、工具清单、两项设置。

流式的那一套（没配模型要在开流之前 501、断开时亲手关生成器）沿用 AI 抽屉的
（app/api/chat.py 的 _ClosingStream / _frame / _resolve_or_501）。
"""
from __future__ import annotations

import logging
import threading
from typing import Iterator

from fastapi import APIRouter, Body

from app.agent import files, loop, tools
from app.api.chat import _ClosingStream, _frame, _resolve_or_501
from app.api.common import ApiError, guard
from app.storage import agent_sessions as sessions
from app.storage import db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

PERMISSIONS = ("ask", "auto")
DRIVERS = ("auto", "toolcall", "action")

# 同一个会话同一时间只跑一轮 —— 两轮交错着往同一份上下文里写，存回去的是哪份就说不清了
_busy: set[str] = set()
_busy_lock = threading.Lock()


def _settings() -> dict:
    p = db.get_setting("agent.permission", "ask")
    d = db.get_setting("agent.driver", "auto")
    return {"permission": p if p in PERMISSIONS else "ask", "driver": d if d in DRIVERS else "auto",
            "roots": files.roots()}


@router.get("/tools")
def list_tools() -> dict:
    return {"tools": [t.public() for t in tools.TOOLS],
            "limits": {"max_turns": loop.MAX_TURNS, "max_seconds": loop.MAX_SECONDS, "task_wait_max": tools.TASK_WAIT_MAX,
                       "tool_max_chars": tools.TOOL_MAX_CHARS, "compact_at_chars": loop.COMPACT_AT_CHARS}}


@router.get("/settings")
def get_settings() -> dict:
    return _settings()


@router.post("/settings")
def save_settings(payload: dict = Body(...)) -> dict:
    if "permission" in payload:
        if payload["permission"] not in PERMISSIONS:
            raise ApiError(f"permission 只能是 {' / '.join(PERMISSIONS)}", 400)
        db.set_setting("agent.permission", payload["permission"])
    if "driver" in payload:
        if payload["driver"] not in DRIVERS:
            raise ApiError(f"driver 只能是 {' / '.join(DRIVERS)}", 400)
        db.set_setting("agent.driver", payload["driver"])
    if "roots" in payload:
        # 智能体能碰的文件夹（工作区之外的那些）：[{path, write}]。工作区总在，不用传
        raw = payload["roots"] or []
        if not isinstance(raw, list) or len(raw) > 20:
            raise ApiError("roots 要是列表，最多 20 个", 400)
        clean, seen = [], set()
        for r in raw:
            if not isinstance(r, dict):
                raise ApiError("roots 的每一项要是 {path, write}", 400)
            try:
                path = files.check_new_root(str(r.get("path") or ""))
            except files.FileToolError as exc:
                raise ApiError(str(exc), 400, "bad_root") from exc
            if path in seen:
                continue
            seen.add(path)
            clean.append({"path": path, "write": bool(r.get("write"))})
        db.set_setting("agent.roots", clean)
    return _settings()


@router.get("/file-changes")
def file_changes(session_id: str = "") -> dict:
    return {"changes": files.changes(session_id or None, None, 100)}


@router.post("/file-changes/{change_id}/rollback")
def rollback_change(change_id: str, payload: dict = Body(default={})) -> dict:
    """界面上「撤销这次改动」—— 是你点的，不经过模型、也不用再批。"""
    try:
        return files.rollback(change_id, None, bool(payload.get("force")))
    except files.FileToolError as exc:
        raise ApiError(str(exc), 400, "rollback_failed") from exc


@router.post("/sessions/{session_id}/stop")
def stop(session_id: str) -> dict:
    """「停止」：回路在下一步之间、等任务的下一秒就停。前端点停止时先调它再断开连接。"""
    guard(sessions.get, session_id)
    if session_id in _busy:
        loop.request_stop(session_id)
    return {"ok": True, "running": session_id in _busy}


@router.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": sessions.list_all()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    s = guard(sessions.get, session_id)
    return {"session_id": s["session_id"], "title": s["title"], "model": s["model"],
            "events": s["events"], "remembered": s["remembered"],
            "pending": [{k: c[k] for k in ("id", "name", "args", "risk", "preview")}
                        for c in (s["pending"] or {}).get("calls", [])],
            "busy": session_id in _busy}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    if session_id in _busy:
        raise ApiError("这个会话正在跑，先停下来再删", 409, "busy")
    guard(sessions.delete, session_id)
    return {"ok": True}


def _stream(session_id: str, gen_kwargs: dict) -> Iterator[bytes]:
    """把回路的事件转成 SSE 帧。这里不能把异常抛给框架（响应头早就发了），一律转成 error 帧。"""
    with _busy_lock:
        if session_id in _busy:
            yield _frame("error", {"message": "这个会话正在跑，等它停下来再说"})
            return
        _busy.add(session_id)
    try:
        s = sessions.get(session_id)
        yield _frame("session", {"session_id": session_id, "title": s["title"]})
        gen = loop.run(session_id, **gen_kwargs)
        try:
            for e in gen:
                yield _frame(e["type"], e)
        except Exception as exc:                # noqa: BLE001
            log.exception("智能体出错")
            yield _frame("error", {"message": f"智能体出错：{exc}"})
        finally:
            gen.close()
    finally:
        with _busy_lock:
            _busy.discard(session_id)


def _sse(session_id: str, gen_kwargs: dict) -> _ClosingStream:
    return _ClosingStream(_stream(session_id, gen_kwargs), media_type="text/event-stream",
                          headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/run")
def run(payload: dict = Body(...)):
    """说一句话。没有 session_id 就新建一个 —— 等第一句话才建，不攒空会话（L-1 的教训）。"""
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ApiError("请输入要做的事", 400)
    if len(prompt) > 4000:
        raise ApiError("一次说的太长了（超过 4000 字）", 400)
    sid = payload.get("session_id") or ""
    if sid:
        guard(sessions.get, sid)
        if sid in _busy:
            raise ApiError("这个会话正在跑，等它停下来再说", 409, "busy")
    # ★ 没配模型要在开流之前报
    provider, model = _resolve_or_501(payload.get("provider"), payload.get("model"))
    if not sid:
        sid = sessions.create()
    st = _settings()
    return _sse(sid, {"provider": provider, "model": model, "prompt": prompt,
                      "permission": st["permission"], "mode": st["driver"]})


@router.post("/sessions/{session_id}/resume")
def resume(session_id: str, payload: dict = Body(...)):
    """批完接着跑：{decisions: {调用id: approve|reject}, remember: [工具名]}。没提到的调用当作拒绝。"""
    s = guard(sessions.get, session_id)
    if not s["pending"]:
        raise ApiError("没有在等确认的操作（可能已经批过了）", 409, "no_pending")
    if session_id in _busy:
        raise ApiError("这个会话正在跑", 409, "busy")
    decisions = payload.get("decisions") or {}
    if not isinstance(decisions, dict):
        raise ApiError("decisions 必须是 {调用id: approve|reject}", 400)
    provider, model = _resolve_or_501(payload.get("provider"), payload.get("model"))
    st = _settings()
    return _sse(session_id, {"provider": provider, "model": model,
                             "resume": {"decisions": decisions, "remember": payload.get("remember") or []},
                             "permission": st["permission"], "mode": st["driver"]})
