"""AI 抽屉的后端：会话、流式回答、结构化动作卡片、钉住。

跟 `app/api/assist.py` 的分工：assist 只剩「模型配好了没有」的状态接口；
这里是右侧抽屉，有历史、有数据范围、有能落到界面上的动作。

三件值得单独说的事：

1. **501 必须在开流之前发。** FastAPI 的 StreamingResponse 一旦开始产出，
   状态码就已经在路上了，`main.py` 里那个全局异常处理器再也插不上手。
   所以「没配模型」这类错在 `resolve_or_501`（app/api/streaming.py）里同步抛，
   流中途才出的错走 `event: error` 帧。
2. **用户那条消息先落库再开流。** 流断了、浏览器关了、模型崩了，
   问过的话都还在。答到一半的也存半截 + `aborted` 标记。
3. **模型写的动作永远不自动执行。** 它只能吐一个 json，界面渲染成
   你看得见、能改、能拒绝的卡片。
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

from fastapi import APIRouter, Body, Query

from app import config
from app.ai import assistant
from app.ai import context as ai_context
from app.ai.provider import ChatMessage, ProviderUnavailable
from app.api.common import ApiError, guard
from app.api.streaming import ClosingStream, resolve_or_501, sse_frame
from app.storage import conversations

log = logging.getLogger("hte.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 界面带上来的那段说明能有多长。定义原文只有两三百字，
# 加上「眼前这一处」的读数（最多 12 行）之后要留出余量。
CONTEXT_NOTE_MAX = 4000

# 对比 run 的 id 形状（`db.new_id("run")`）。scope 里放 id 而不是快照，
# 就得确认它真是个 id。
_RUN_ID_RE = re.compile(r"run_[0-9a-f]{6,32}")


# ---------------------------------------------------------------- 会话 CRUD
@router.get("/conversations")
def list_conversations(limit: int = Query(50, ge=1, le=200),
                       topic: str = Query("")) -> dict:
    """带 `topic` 就只列这个术语下的对话（术语 ⓘ 弹窗用），不带就是全部。"""
    return {"conversations": conversations.list_recent(limit, topic=topic or None)}


@router.post("/conversations")
def create_conversation(payload: dict = Body(default={})) -> dict:
    cid = conversations.create(
        title=(payload.get("title") or "新对话").strip()[:120],
        scope=_clean_scope(payload.get("scope")))
    return {"conversation": conversations.get(cid), "messages": []}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    conv = conversations.get(conversation_id)
    if not conv:
        raise ApiError("没有这个对话", 404, "not_found")
    return {"conversation": conv, "messages": conversations.messages(conversation_id)}


@router.patch("/conversations/{conversation_id}")
def patch_conversation(conversation_id: str, payload: dict = Body(...)) -> dict:
    if not conversations.get(conversation_id):
        raise ApiError("没有这个对话", 404, "not_found")
    if "title" in payload:
        conversations.rename(conversation_id, str(payload["title"]))
    if "scope" in payload:
        conversations.set_scope(conversation_id, _clean_scope(payload["scope"]))
    return {"conversation": conversations.get(conversation_id)}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict:
    if not conversations.delete(conversation_id):
        raise ApiError("没有这个对话", 404, "not_found")
    return {"ok": True}


# ---------------------------------------------------------------- 范围预览
@router.post("/scope/preview")
def scope_preview(payload: dict = Body(default={})) -> dict:
    """抽屉里那个范围选择器的数字从这儿来。

    不调模型，纯查库 —— 切换「选中的 / 全部的」不该等模型。
    """
    scope = _clean_scope(payload.get("scope"))
    built = guard(ai_context.build, scope)
    return {
        "n_samples": built["n_samples"],
        "needs_narrowing": built["needs_narrowing"],
        "detail_max": config.AI_DETAIL_MAX,
        "mode": built["mode"],
    }


# ---------------------------------------------------------------- 发消息（流式）
@router.post("/conversations/{conversation_id}/messages")
def post_message(conversation_id: str, payload: dict = Body(...)):
    conv = conversations.get(conversation_id)
    if not conv:
        raise ApiError("没有这个对话", 404, "not_found")

    question = (payload.get("content") or "").strip()
    if not question:
        raise ApiError("请输入问题", 400)

    # 范围：这次带的优先，没带就用会话上存的
    scope = _clean_scope(payload.get("scope")) or conv.get("scope") or {}
    if payload.get("scope") is not None:
        conversations.set_scope(conversation_id, scope)

    # ★ 没配模型要在**开流之前**炸。开了流就再也改不了状态码了。
    provider, model = resolve_or_501(payload.get("provider"), payload.get("model"))

    # 术语对话默认不给逐样品明细：用户问的是「DEGRADED 是什么意思」，
    # 塞 40 行样品表既挤掉定义本身，又会诱着模型去提议收窄筛选式。
    #
    # **但 ⓘ 明确说了在看哪几个样品时例外。** 那条理由是「40 行帮不上忙」——
    # 范围已经窄到一两个样品时它不成立，而那几行恰恰就是问题本身。
    topic = scope.get("topic") or ""
    narrowed = bool((scope.get("filter") or {}).get("ids"))
    built = guard(ai_context.build, scope,
                  overview_only=bool(topic) and not narrowed,
                  run_id=scope.get("run_id") or "")
    facts = ai_context.to_prompt(built["facts"])

    # 术语对话要把「当时看的是哪一处」一起回放。那几个数只在 system 里给过一次，
    # 不进正文 —— 不拼回去的话，「为什么？」这种追问到模型那儿是一句
    # 光秃秃的「为什么这一秒全不可信」，它不知道是哪一秒。
    history = conversations.history_for_model(conversation_id, with_spot=bool(topic))
    spot = str(payload.get("spot") or "").strip()[:200]
    conversations.add_message(
        conversation_id, "user", question,
        meta={"scope": scope, "n_samples": built["n_samples"], **({"spot": spot} if spot else {})})
    if conv["title"] == "新对话":
        conversations.rename(conversation_id, question[:40])

    system = assistant.SYSTEM_PROMPT
    if topic:
        # 定义原文由前端带上来 —— 术语表是界面文案，跟界面放在一起才不会漂。
        # 两份定义各自维护迟早对不上，那时候用户看到的和模型说的就不是一回事了。
        note = str(payload.get("context_note") or "").strip()[:CONTEXT_NOTE_MAX]
        system = assistant.GLOSSARY_PROMPT
        if note:
            system += f"\n\n界面上这条术语的定义原文：\n{note}\n"

    messages = [ChatMessage("system", system)]
    messages += [ChatMessage(m["role"], m["content"]) for m in history]
    messages.append(ChatMessage("user", f"已知事实：\n{facts}\n\n我的问题：{question}"))

    assistant_id = conversations.add_message(
        conversation_id, "assistant", "",
        meta={"model": model, "provider": provider.name, "pending": True})

    return ClosingStream(
        _stream(provider, model, messages, conversation_id, assistant_id, built),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream(provider, model, messages, conversation_id, assistant_id,
            built: dict) -> Iterator[bytes]:
    """产出 SSE 帧。

    这个生成器里**不能抛异常给框架** —— 响应头早就发出去了，抛上去用户看到的是
    连接莫名其妙断掉。所有错都转成 `event: error` 帧，界面能照实显示。
    """
    yield sse_frame("meta", {"message_id": assistant_id, "model": model,
                          "provider": provider.name,
                          "n_samples": built["n_samples"],
                          "needs_narrowing": built["needs_narrowing"]})

    chunks: list[str] = []
    error: str | None = None
    finished = False
    stream = provider.chat_stream(messages, model=model, temperature=0.3, max_tokens=2000)
    try:
        try:
            for piece in stream:
                chunks.append(piece)
                yield sse_frame("delta", {"text": piece})
        except ProviderUnavailable as exc:
            error = str(exc)
        except Exception as exc:                   # noqa: BLE001 — 见上面的说明
            log.exception("流式回答出错")
            error = f"模型调用出错：{exc}"
        finished = True
    finally:
        if not finished:
            # 你点了「停止」、关了页面、断了网：框架在上面的 yield 那儿把这个生成器关掉
            # （GeneratorExit），下面的代码一行都不会跑。原来存消息就在下面 ——
            # 被打断的回答永远是一条空白的「生成中」（自查 §6 #16）。
            # 这里不能再 yield，只把已经说出来的存下来，并关掉模型那头的流（别白占着网关）。
            getattr(stream, "close", lambda: None)()
            conversations.update_message(assistant_id, "".join(chunks), meta={
                "model": model, "provider": provider.name,
                "scope_n_samples": built["n_samples"], "aborted": True})

    text = "".join(chunks)
    card = assistant.card_from(text) if text else None
    meta = {"model": model, "provider": provider.name,
            "scope_n_samples": built["n_samples"]}
    if card:
        meta["card"] = card
    if error:
        meta["error"] = error
    conversations.update_message(assistant_id, text, meta=meta)

    if error:
        yield sse_frame("error", {"message": error, "kind": "model_failed",
                               "partial": bool(text)})
        return
    if card:
        yield sse_frame("card", card)
    yield sse_frame("done", {"message_id": assistant_id, "chars": len(text)})


# ---------------------------------------------------------------- 钉住
@router.post("/pins")
def create_pin(payload: dict = Body(...)) -> dict:
    run_id = (payload.get("analysis_run_id") or "").strip()
    note = (payload.get("note") or "").strip()
    if not run_id:
        raise ApiError("请选择要钉到哪一次对比", 400)
    if not note:
        raise ApiError("空的分析没什么好钉的", 400)
    pid = guard(conversations.pin, run_id, payload.get("conversation_id"), payload.get("message_id"), note)
    return {"pin_id": pid, "pins": conversations.pins_for(run_id)}


@router.get("/pins")
def list_pins(run: str = Query("", alias="run")) -> dict:
    if run:
        return {"pins": conversations.pins_for(run)}
    return {"counts": conversations.pin_counts()}


@router.delete("/pins/{pin_id}")
def delete_pin(pin_id: str) -> dict:
    if not conversations.unpin(pin_id):
        raise ApiError("没有这条钉住的分析", 404, "not_found")
    return {"ok": True}


# ---------------------------------------------------------------- 内部
def _clean_scope(raw) -> dict:
    """范围只留认识的几个键（mode、filter、label、topic、run_id），filter 走 selection 自己的校验。

    这里不接受 ID 列表以外的任何自由结构 —— 范围一旦能塞进任意 json，
    以后就会有人往里塞快照，然后历史对话点开是一批不存在的样品。
    """
    if not isinstance(raw, dict):
        return {}
    from app.storage import selection

    mode = raw.get("mode")
    out: dict = {"mode": mode if mode in ("selected", "all") else "all"}
    flt = raw.get("filter")
    if isinstance(flt, dict) and flt:
        try:
            out["filter"] = selection.normalize(flt)
        except (ValueError, TypeError) as exc:
            raise ApiError(f"数据范围不合法：{exc}", 400, "invalid") from exc
    else:
        out["filter"] = {}
    if raw.get("label"):
        out["label"] = str(raw["label"])[:120]
    # 术语 ⓘ 弹窗：一条术语一条对话线。只是个标签，不参与查询。
    if raw.get("topic"):
        out["topic"] = str(raw["topic"])[:80]
    # 在对比页上点开的 ⓘ 会带上**是哪一次**对比。
    #
    # 它是 id 不是快照 —— 每次读的时候重新去库里解析，那次对比要是被删了，
    # 就是查不到，而不是拿着一份早就对不上的旧数字继续说话。
    # 这正是上面那条规矩想要的形状，所以放行。
    run_id = str(raw.get("run_id") or "").strip()
    if run_id:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ApiError(f"不像是一次对比的 id：{run_id}", 400, "invalid")
        out["run_id"] = run_id
    return out
