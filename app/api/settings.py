"""设置：导入策略、命名规则、模型配置。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Query

from app import config
from app.ai import openai_compat
from app.ai.provider import ProviderUnavailable
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
    """模型配置概览。密钥在这里被打码，绝不原样回传前端。

    `presets` 是下拉里那份候选地址：**自己存的排在前面**，
    几个公共服务商跟在后面（给第一次打开设置页的人用）。

    自己那份存在 workspace/config/providers.json 里，和密钥同一个文件、
    同一条 .gitignore —— **内网地址不进仓库**，仓库里那份只有公共网关。
    """
    out = openai_compat.describe_config()
    out["presets"] = openai_compat.all_presets()
    return out


@router.post("/models/presets")
def add_preset(payload: dict = Body(...)) -> dict:
    """把当前这个地址存成候选。

    原来候选清单是写死在代码里的六条，你没有任何办法把自己的网关加进去 ——
    每次配都得重新贴一遍地址。现在填一次，以后下拉里就有。
    """
    try:
        openai_compat.save_preset(payload.get("name") or "",
                                  payload.get("base_url") or "")
    except ValueError as exc:
        raise ApiError(str(exc), 400) from exc
    return {"presets": openai_compat.all_presets()}


@router.delete("/models/presets")
def remove_preset(base_url: str = Query(..., description="要去掉的地址")) -> dict:
    """从本机名单里去掉一个地址。内置的那几条去不掉 —— 它们不在这个文件里。"""
    openai_compat.delete_preset(base_url)
    return {"presets": openai_compat.all_presets()}


@router.post("/models/discover")
def discover_models(payload: dict = Body(...)) -> dict:
    """按地址拉一份可用模型列表，省得手打模型名。

    **密钥留空 = 沿用已经存着的那个**（和 /models/simple 同一条规则）：
    界面上只显示打码后的密钥，用户手里未必还有原文，不该为了拉个列表
    被迫重新贴一遍。

    拉不到不是故障 —— 有的网关根本没实现 /models。界面上要保留手填那条路。
    """
    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    api_key = (payload.get("api_key") or "").strip()
    if not base_url:
        raise ApiError("请先填接口地址", 400)

    if not api_key:
        try:
            providers = (openai_compat.load_config().get("providers") or {})
        except ProviderUnavailable:
            providers = {}
        old = next((p for p in providers.values()
                    if (p.get("baseUrl") or "").rstrip("/") == base_url), None)
        api_key = (old or {}).get("apiKey") or ""

    try:
        found = openai_compat.list_remote_models(base_url, api_key)
    except ProviderUnavailable as exc:
        raise ApiError(str(exc), 502, "discover_failed") from exc

    # 返回里**没有** api_key，一个字段都不带。这条由 test_api_key_is_never_returned
    # 那一族测试盯着。
    return {"base_url": base_url, "models": found, "count": len(found),
            "used_saved_key": not (payload.get("api_key") or "").strip() and bool(api_key)}


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


@router.post("/models/simple")
def save_simple_model(payload: dict = Body(...)) -> dict:
    """三个框存一个 provider：地址 + 密钥 + 模型名。

    绝大多数人只有一个网关一个模型，让他们为了填一个地址先去读一段 JSON
    结构，是把内部实现当成了使用说明。JSON 那条路留着（/models），
    需要多 provider 时才用得上。

    **密钥留空 = 不改。** 改地址、换模型名的时候不该被迫把密钥再贴一遍 ——
    界面上只显示打码后的密钥，用户手里也未必还有原文。
    """
    name = (payload.get("name") or "我的模型").strip()[:60]
    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    api_key = (payload.get("api_key") or "").strip()

    # 一次可以存多个模型：从 /models/discover 那个列表里勾几个，
    # 之后在设置页和对话界面都能切。单个的老写法（model_id）继续认。
    raw_ids = payload.get("model_ids")
    if isinstance(raw_ids, list):
        model_ids = [str(m).strip() for m in raw_ids if str(m).strip()]
    else:
        model_ids = [(payload.get("model_id") or "").strip()]
    model_ids = [m for m in dict.fromkeys(model_ids) if m]     # 去重且保序

    if not base_url:
        raise ApiError("请填接口地址（baseUrl）", 400)
    if not base_url.startswith(("http://", "https://")):
        raise ApiError("接口地址要以 http:// 或 https:// 开头", 400)

    try:
        current = openai_compat.load_config()
    except ProviderUnavailable:
        current = {"providers": {}}       # 现有文件坏了也要能救回来
    providers = dict(current.get("providers") or {})

    if not api_key:
        # 沿用已经存着的那个。先按名字找，找不到就按地址找 —— 用户可能改了名字
        old = providers.get(name) or next(
            (p for p in providers.values()
             if (p.get("baseUrl") or "").rstrip("/") == base_url), None)
        api_key = (old or {}).get("apiKey") or ""
        if not api_key:
            raise ApiError("这是第一次配置，密钥不能留空", 400)

    # **模型列表留空 = 不改**，和密钥同一条规则。
    #
    # 原来这里要求「至少选一个模型」才让保存 —— 可自建网关很多不实现
    # /models，拉不到列表就卡在这儿，地址想先存下来都不行。
    # 而改地址、改名字的时候更没道理被迫把模型再勾一遍。
    if not model_ids:
        old = providers.get(name) or next(
            (p for p in providers.values()
             if (p.get("baseUrl") or "").rstrip("/") == base_url), None)
        model_ids = [m.get("id") for m in ((old or {}).get("models") or [])
                     if m.get("id")]

    providers[name] = {
        "baseUrl": base_url,
        "api": "openai-completions",
        "apiKey": api_key,
        # 大多数自建网关不认 developer 角色，默认按不支持处理，降级成 system
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "models": [{"id": m, "name": m, "input": ["text"]} for m in model_ids],
    }

    config.ensure_dirs()
    # 连同 providers 之外的顶层键一起写回 —— 地址预设就存在那儿，
    # 只写 {"providers": ...} 会把它们一并抹掉。
    openai_compat.save_config({**current, "providers": providers})
    return openai_compat.describe_config()


@router.post("/models/test")
def test_models(payload: dict = Body(default={})) -> dict:
    return openai_compat.test_connection(payload.get("provider"), payload.get("model"))


@router.get("/models/example")
def example_config() -> dict:
    p = config.PROVIDERS_EXAMPLE_PATH
    return {"example": p.read_text(encoding="utf-8") if p.is_file() else "{}"}
