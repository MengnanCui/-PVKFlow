"""「AI 分析」面板的后端。

规则引擎永远先跑，模型只在配置了的时候补一层解释与总结。
返回结构里 `source` 字段明确标出每部分结论从哪来，界面照实显示。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body

from app.ai import openai_compat, rules
from app.ai.provider import ChatMessage, ProviderUnavailable
from app.api.common import ApiError, guard

router = APIRouter(prefix="/api/assist", tags=["assist"])


@router.get("/status")
def status() -> dict:
    """界面据此决定是显示「规则」还是模型名。"""
    models = openai_compat.list_models()
    active = None
    if models:
        try:
            provider, model_id = openai_compat.resolve()
            active = {"provider": provider.name, "model": model_id}
        except ProviderUnavailable:
            active = None
    return {
        "model_configured": bool(models),
        "active": active,
        "models": [m.as_dict() for m in models],
        "rules_available": True,
    }


@router.post("/inspect")
def inspect(payload: dict = Body(...)) -> dict:
    """规则版分析：认文件、排 skill、标异常。永远可用。"""
    ids = payload.get("artifact_ids") or []
    if not ids:
        raise ApiError("请先选择文件", 400)
    return guard(rules.assist, ids)


@router.post("/ask")
def ask(payload: dict = Body(...)) -> dict:
    """把当前上下文 + 用户的问题交给模型。

    没配模型时返回 501 和一句人话，前端照实显示，不编造回答。
    """
    question = (payload.get("question") or "").strip()
    if not question:
        raise ApiError("请输入问题", 400)

    ids = payload.get("artifact_ids") or []
    context = guard(rules.assist, ids) if ids else {"files": [], "issues": [], "suggestions": []}

    try:
        provider, model = openai_compat.resolve(payload.get("provider"), payload.get("model"))
    except ProviderUnavailable as exc:
        raise ApiError(str(exc), 501, "no_model") from exc

    system = (
        "你是高通量实验数据平台里的分析助手。用户正在处理实验数据文件。\n"
        "下面是平台的规则引擎已经确定的事实（文件类型、列名、数据质量问题、候选处理工具）。"
        "请基于这些事实回答，不要编造数据里没有的数字。\n"
        "如果规则引擎的判断看起来不对，明确指出来。回答用中文，简洁，不要客套。"
    )
    facts = json.dumps(_slim(context), ensure_ascii=False, indent=2)
    extra = payload.get("result_context")
    if extra:
        facts += "\n\n最近一次处理的结果：\n" + json.dumps(extra, ensure_ascii=False)[:4000]

    try:
        reply = provider.chat(
            [ChatMessage("system", system),
             ChatMessage("user", f"已知事实：\n{facts}\n\n我的问题：{question}")],
            model=model, temperature=0.3, max_tokens=1500,
        )
    except ProviderUnavailable as exc:
        raise ApiError(str(exc), 502, "model_failed") from exc

    return {
        "answer": reply.text,
        "source": "model",
        "provider": reply.provider,
        "model": reply.model,
        "usage": reply.usage,
        "context": context,
    }


def _slim(context: dict) -> dict:
    """喂给模型前砍掉冗余，省 token。"""
    files = []
    for f in context.get("files", []):
        files.append({k: v for k, v in f.items()
                      if k in ("filename", "kind", "domain", "columns", "dtypes",
                               "delimiter", "encoding", "issues")})
    return {
        "files": files,
        "suggested_skills": context.get("suggestions", []),
    }
