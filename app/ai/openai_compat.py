"""OpenAI 兼容协议客户端（vLLM / 各家网关 / OpenAI 本身）。

配置文件放 workspace/config/providers.json，结构：

    {
      "providers": {
        "vllm-local": {
          "baseUrl": "http://.../v1",
          "api": "openai-completions",
          "apiKey": "sk-...",
          "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
          "models": [
            { "id": "Qwen3.6-27B", "name": "Qwen 3.6 27B (Local)",
              "input": ["text","image"], "contextWindow": 101072, "maxTokens": 65535 }
          ]
        }
      }
    }

这个文件含密钥，在 .gitignore 里，永远不会被提交。仓库里只有
config/providers.example.json 这个占位模板。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app import config
from app.ai.provider import ChatMessage, ChatResult, ProviderUnavailable

DEFAULT_TIMEOUT = 120.0


@dataclass
class ModelInfo:
    id: str
    name: str = ""
    provider: str = ""
    context_window: int = 0
    max_tokens: int = 0
    inputs: tuple[str, ...] = ("text",)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name or self.id, "provider": self.provider,
                "context_window": self.context_window, "max_tokens": self.max_tokens,
                "inputs": list(self.inputs), "vision": "image" in self.inputs}


def _redact(key: str) -> str:
    if not key:
        return ""
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


def load_config(path: Path | None = None) -> dict:
    p = path or config.PROVIDERS_PATH
    if not p.is_file():
        return {"providers": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ProviderUnavailable(f"providers.json 解析失败：{exc}") from exc
    if "providers" not in data and isinstance(data, dict):
        data = {"providers": data}       # 容忍少写一层
    return data


def save_config(data: dict, path: Path | None = None) -> None:
    p = path or config.PROVIDERS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_models(path: Path | None = None) -> list[ModelInfo]:
    out: list[ModelInfo] = []
    try:
        cfg = load_config(path)
    except ProviderUnavailable:
        return out
    for pname, pcfg in (cfg.get("providers") or {}).items():
        for m in pcfg.get("models") or []:
            out.append(ModelInfo(
                id=m.get("id", ""),
                name=m.get("name", ""),
                provider=pname,
                context_window=int(m.get("contextWindow") or 0),
                max_tokens=int(m.get("maxTokens") or 0),
                inputs=tuple(m.get("input") or ("text",)),
            ))
    return [m for m in out if m.id]


def describe_config(path: Path | None = None) -> dict:
    """给设置页看的配置概览。密钥打码，绝不原样回传到前端。"""
    try:
        cfg = load_config(path)
    except ProviderUnavailable as exc:
        return {"ok": False, "error": str(exc), "providers": []}
    providers = []
    for pname, pcfg in (cfg.get("providers") or {}).items():
        providers.append({
            "name": pname,
            "base_url": pcfg.get("baseUrl", ""),
            "api": pcfg.get("api", "openai-completions"),
            "api_key_masked": _redact(pcfg.get("apiKey", "")),
            "has_key": bool(pcfg.get("apiKey")),
            "models": [ModelInfo(
                id=m.get("id", ""), name=m.get("name", ""), provider=pname,
                context_window=int(m.get("contextWindow") or 0),
                max_tokens=int(m.get("maxTokens") or 0),
                inputs=tuple(m.get("input") or ("text",)),
            ).as_dict() for m in (pcfg.get("models") or [])],
        })
    return {"ok": True, "path": str(path or config.PROVIDERS_PATH), "providers": providers}


class OpenAICompatProvider:
    """一个 provider 条目对应一个实例。"""

    def __init__(self, name: str, cfg: dict) -> None:
        self.name = name
        self.base_url = (cfg.get("baseUrl") or "").rstrip("/")
        self.api_key = cfg.get("apiKey") or ""
        self.compat = cfg.get("compat") or {}
        self.models = [m.get("id") for m in (cfg.get("models") or []) if m.get("id")]
        self.default_model = self.models[0] if self.models else None

    def available(self) -> bool:
        return bool(self.base_url and self.default_model)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _normalize(self, messages: list[ChatMessage]) -> list[dict]:
        out = []
        for m in messages:
            role = m.role
            # 有些网关不支持 developer 角色，降级成 system
            if role == "developer" and not self.compat.get("supportsDeveloperRole", False):
                role = "system"
            out.append({"role": role, "content": m.content})
        return out

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ChatResult:
        if not self.available():
            raise ProviderUnavailable(f"provider「{self.name}」配置不完整（缺 baseUrl 或 models）")

        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._normalize(messages),
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"连接 {self.name} 失败：{exc}") from exc

        if resp.status_code >= 400:
            raise ProviderUnavailable(
                f"{self.name} 返回 {resp.status_code}：{resp.text[:400]}"
            )

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable(f"{self.name} 返回结构异常：{str(data)[:300]}") from exc

        return ChatResult(text=text, model=payload["model"], provider=self.name,
                          usage=data.get("usage") or {}, raw=data)


def resolve(provider_name: str | None = None, model: str | None = None):
    """按「设置里选的 → 配置里第一个」的顺序找一个可用 provider。

    返回 (provider, model_id)。找不到就抛 ProviderUnavailable，
    调用方负责退回规则引擎。
    """
    from app.storage import db

    cfg = load_config()
    providers = cfg.get("providers") or {}
    if not providers:
        raise ProviderUnavailable(
            "尚未配置模型。把你的 providers 配置粘贴到「设置 → 模型」，或写入 "
            f"{config.PROVIDERS_PATH}"
        )

    pname = provider_name or db.get_setting("active_provider")
    mname = model or db.get_setting("active_model")

    if pname and pname in providers:
        p = OpenAICompatProvider(pname, providers[pname])
        return p, (mname if mname in p.models else p.default_model)

    # 没指定就用配置里第一个能用的
    for name, pcfg in providers.items():
        p = OpenAICompatProvider(name, pcfg)
        if p.available():
            return p, (mname if mname in p.models else p.default_model)

    raise ProviderUnavailable("配置里没有可用的 provider（检查 baseUrl 和 models）")


def test_connection(provider_name: str | None = None, model: str | None = None) -> dict:
    """设置页的「测试连接」。返回结构固定，失败也不抛。"""
    import time

    try:
        provider, model_id = resolve(provider_name, model)
    except ProviderUnavailable as exc:
        return {"ok": False, "error": str(exc)}

    t0 = time.perf_counter()
    try:
        r = provider.chat(
            [ChatMessage("user", "回复两个字：可用")],
            model=model_id, temperature=0, max_tokens=16, timeout=30.0,
        )
    except ProviderUnavailable as exc:
        return {"ok": False, "provider": provider.name, "model": model_id, "error": str(exc)}
    return {
        "ok": True, "provider": provider.name, "model": model_id,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "reply": r.text.strip()[:100],
    }
