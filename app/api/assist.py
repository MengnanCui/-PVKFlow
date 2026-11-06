"""模型配好了没有。AI 抽屉和 ⓘ 浮层据此决定能不能提问、显示哪个模型。

原来这里还有「规则引擎认文件、推荐 skill」和「对着几个文件问一句」两个接口，
随旧 Skill 机制一起退役了（2026-11）：界面上早就没有入口。对话走 app/api/chat.py。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.ai import openai_compat
from app.ai.provider import ProviderUnavailable

router = APIRouter(prefix="/api/assist", tags=["assist"])


@router.get("/status")
def status() -> dict:
    """界面据此决定 AI 能不能用、显示哪个模型。"""
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
    }
