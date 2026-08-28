"""AI 抽屉的后端：会话、流式回答、结构化动作卡片、钉住。

跟 `app/api/assist.py` 的分工：assist 是单文件页里那个「规则引擎 + 一问一答」
的小面板，无状态；这里是右侧抽屉，有历史、有数据范围、有能落到界面上的动作。

三件值得单独说的事：

1. **501 必须在开流之前发。** FastAPI 的 StreamingResponse 一旦开始产出，
   状态码就已经在路上了，`main.py` 里那个全局异常处理器再也插不上手。
   所以「没配模型」这类错在 `_resolve_or_501` 里同步抛，
   流中途才出的错走 `event: error` 帧。
2. **用户那条消息先落库再开流。** 流断了、浏览器关了、模型崩了，
   问过的话都还在。答到一半的也存半截 + `aborted` 标记。
3. **模型写的动作永远不自动执行。** 它只能吐一个 json，界面渲染成
   你看得见、能改、能拒绝的卡片。
"""
from __future__ import annotations

import json
import logging
from typing import Iterator

from fastapi import APIRouter, Body, Query
from fastapi.responses import StreamingResponse

from app import config
from app.ai import context as ai_context
from app.ai import openai_compat
from app.ai.provider import ChatMessage, ProviderUnavailable, extract_json
from app.api.common import ApiError, guard
from app.storage import conversations

log = logging.getLogger("hte.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 模型可以吐的动作。写死成白名单 —— 模型编一个 "delete_everything" 出来时，
# 界面应该当它没说，而不是照着渲染一个按钮。
ACTIONS = ("none", "narrow", "select", "process")

SYSTEM_PROMPT = """\
你是高通量实验数据平台 HTE Studio 里的分析助手。用户是钙钛矿/TCO 方向的研究者，\
正在处理原位吸收光谱与膜厚数据。

下面会给你「已知事实」——平台从数据库里查出来的真实数字。规则：

- 只根据已知事实回答。事实里没有的数字**不要编**，就说数据里没有。
- 事实里如果写了「已截断」或「没有逐个样品的明细」，就说明你看到的不是全部，
  不要下全局结论。
- 回答用中文，简洁，不要客套话，不要复述问题。
- 膜厚相关的判断要记住：光学厚度 OT = f/2，窗口越窄频率分辨率越差；
  条纹数不足时平台会打 LOW_CYCLES / DEGRADED 标志，那些数值不能当准确值用。

如果用户的意图是**操作平台**而不是提问，在回答的最后附一个 json 代码块：

```json
{"action": "select", "filter": {...}, "why": "一句话说明"}
```

可用的 action：
- `narrow`：命中样品太多，你需要先缩小范围。filter 里给收窄条件。
- `select`：用户想挑一批样品。**只给筛选式，绝对不要给样品 ID 列表。**
  可用的键：batch（样品号数组）、folder（文件夹数组）、time（{from,to} ISO 时间）、
  name_prefix、q（模糊搜索）、has_matrix（布尔）。
- `process`：用户想跑批处理。给 filter 和 recipe（band_min/band_max 等）。

没有操作意图就不要附 json。附了也不会自动执行 —— 界面会渲染成一张卡片，
用户改过、点过才算数。所以宁可给一个明显偏保守的筛选式，也不要猜。
"""


GLOSSARY_PROMPT = """\
你是 HTE Studio 里的术语解释助手。用户在界面上点开了某个术语旁边的 ⓘ，\
想把这个概念弄明白。

下面会给你两样东西：

1. **平台里这条术语的权威定义** —— 这是界面上正在显示给用户看的原文。\
   你的回答必须和它一致。它没说的你可以补，它说了的不要改口，\
   更不要另编一套定义出来（用户会同时看到两份，对不上就没法用了）。
2. **用户当前库里的汇总统计** —— 用来把抽象定义落到他自己的数据上。\
   这里面没有的数字不要编。

回答用中文，直接，不要客套话。用户问的是概念，不是要你操作平台 ——\
**不要输出 json 动作块。**
"""


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
    provider, model = _resolve_or_501(payload.get("provider"), payload.get("model"))

    # 术语对话不给逐样品明细：用户问的是「DEGRADED 是什么意思」，
    # 塞 40 行样品表既挤掉定义本身，又会诱着模型去提议收窄筛选式。
    topic = scope.get("topic") or ""
    built = guard(ai_context.build, scope, overview_only=bool(topic))
    facts = ai_context.to_prompt(built["facts"])

    history = conversations.history_for_model(conversation_id)
    conversations.add_message(conversation_id, "user", question,
                              meta={"scope": scope, "n_samples": built["n_samples"]})
    if conv["title"] == "新对话":
        conversations.rename(conversation_id, question[:40])

    system = SYSTEM_PROMPT
    if topic:
        # 定义原文由前端带上来 —— 术语表是界面文案，跟界面放在一起才不会漂。
        # 两份定义各自维护迟早对不上，那时候用户看到的和模型说的就不是一回事了。
        note = str(payload.get("context_note") or "").strip()[:2000]
        system = GLOSSARY_PROMPT
        if note:
            system += f"\n\n界面上这条术语的定义原文：\n{note}\n"

    messages = [ChatMessage("system", system)]
    messages += [ChatMessage(m["role"], m["content"]) for m in history]
    messages.append(ChatMessage("user", f"已知事实：\n{facts}\n\n我的问题：{question}"))

    assistant_id = conversations.add_message(
        conversation_id, "assistant", "",
        meta={"model": model, "provider": provider.name, "pending": True})

    return StreamingResponse(
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
    yield _frame("meta", {"message_id": assistant_id, "model": model,
                          "provider": provider.name,
                          "n_samples": built["n_samples"],
                          "needs_narrowing": built["needs_narrowing"]})

    chunks: list[str] = []
    error: str | None = None
    try:
        for piece in provider.chat_stream(messages, model=model,
                                          temperature=0.3, max_tokens=2000):
            chunks.append(piece)
            yield _frame("delta", {"text": piece})
    except ProviderUnavailable as exc:
        error = str(exc)
    except Exception as exc:                       # noqa: BLE001 — 见上面的说明
        log.exception("流式回答出错")
        error = f"模型调用出错：{exc}"

    text = "".join(chunks)
    card = _card_from(text) if text else None
    meta = {"model": model, "provider": provider.name,
            "scope_n_samples": built["n_samples"]}
    if card:
        meta["card"] = card
    if error:
        meta["error"] = error
    conversations.update_message(assistant_id, text, meta=meta)

    if error:
        yield _frame("error", {"message": error, "kind": "model_failed",
                               "partial": bool(text)})
        return
    if card:
        yield _frame("card", card)
    yield _frame("done", {"message_id": assistant_id, "chars": len(text)})


def _frame(event: str, data: dict) -> bytes:
    return (f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")


def _card_from(text: str) -> dict | None:
    """从回答里抠出动作卡片。抠不出来就没有卡片 —— 这是正常情况，不是错误。"""
    if "{" not in text:
        return None                       # 绝大多数回答是纯文字，别白跑一趟解析
    try:
        obj = extract_json(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    if action not in ACTIONS or action == "none":
        return None
    card = {"action": action, "why": str(obj.get("why") or "")[:300]}

    flt = obj.get("filter")
    if isinstance(flt, dict):
        # 模型给了 ID 列表就丢掉那一部分。系统提示里已经说了不要给，
        # 但白名单要在代码里，不能只在提示词里 —— 提示词是建议，代码才是保证。
        flt = {k: v for k, v in flt.items() if k != "ids"}
        try:
            from app.storage import selection
            card["filter"] = selection.normalize(flt)
            card["count"] = selection.count(card["filter"])
        except (ValueError, TypeError) as exc:
            card["filter_error"] = f"模型给的筛选式不合法：{exc}"
            card["filter"] = flt
    if isinstance(obj.get("recipe"), dict):
        card["recipe"] = obj["recipe"]
    return card


# ---------------------------------------------------------------- 钉住
@router.post("/pins")
def create_pin(payload: dict = Body(...)) -> dict:
    run_id = (payload.get("analysis_run_id") or "").strip()
    note = (payload.get("note") or "").strip()
    if not run_id:
        raise ApiError("请选择要钉到哪一次对比", 400)
    if not note:
        raise ApiError("空的分析没什么好钉的", 400)
    pid = conversations.pin(run_id, payload.get("conversation_id"),
                            payload.get("message_id"), note)
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
def _resolve_or_501(provider_name: str | None, model: str | None):
    try:
        return openai_compat.resolve(provider_name, model)
    except ProviderUnavailable as exc:
        raise ApiError(str(exc), 501, "no_model") from exc


def _clean_scope(raw) -> dict:
    """范围只留三个键，filter 走 selection 自己的校验。

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
    return out
