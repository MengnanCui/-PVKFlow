"""设置：导入策略、命名规则、模型配置。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body

from app import config
from app.ai import openai_compat
from app.api.common import ApiError
from app.storage import db

router = APIRouter(prefix="/api/settings", tags=["settings"])

_EDITABLE = {
    "copy_extensions", "reference_extensions", "unknown_policy",
    "naming_rules", "thumbnail_max_px", "max_preview_rows", "cache_limit_gb",
    "active_provider", "active_model",
}


@router.get("")
def get_settings() -> dict:
    return {
        "settings": db.all_settings(),
        "editable": sorted(_EDITABLE),
        "workspace": str(config.WORKSPACE),
        "providers_path": str(config.PROVIDERS_PATH),
        "version": config.APP_VERSION,
    }


@router.post("")
def update_settings(payload: dict = Body(...)) -> dict:
    unknown = set(payload) - _EDITABLE
    if unknown:
        raise ApiError(f"不可修改的设置项：{', '.join(sorted(unknown))}", 400)
    for k, v in payload.items():
        db.set_setting(k, v)
    return {"settings": db.all_settings()}


@router.get("/cache")
def cache_status() -> dict:
    """解析缓存占了多少。上千个样品能到几个 GB，得让人看得见、能清掉。"""
    from app.parsers import matrix

    return matrix.cache_stats()


@router.post("/cache/clear")
def cache_clear() -> dict:
    from app.parsers import matrix

    return matrix.clear_cache()


@router.get("/models")
def models() -> dict:
    """模型配置概览。密钥在这里被打码，绝不原样回传前端。"""
    return openai_compat.describe_config()


@router.post("/models")
def save_models(payload: dict = Body(...)) -> dict:
    """保存 providers 配置。

    写到 workspace/config/providers.json —— 这个路径在 .gitignore 里，
    密钥不会被提交进仓库。
    """
    raw = payload.get("config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"JSON 解析失败：{exc}", 400, "invalid_json") from exc
    if not isinstance(raw, dict):
        raise ApiError("config 必须是一个 JSON 对象", 400)
    if "providers" not in raw:
        raw = {"providers": raw}
    if not isinstance(raw.get("providers"), dict) or not raw["providers"]:
        raise ApiError("配置里没有 providers", 400)

    config.ensure_dirs()
    openai_compat.save_config(raw)
    return openai_compat.describe_config()


@router.post("/models/test")
def test_models(payload: dict = Body(default={})) -> dict:
    return openai_compat.test_connection(payload.get("provider"), payload.get("model"))


@router.get("/models/example")
def example_config() -> dict:
    p = config.PROVIDERS_EXAMPLE_PATH
    return {"example": p.read_text(encoding="utf-8") if p.is_file() else "{}"}
