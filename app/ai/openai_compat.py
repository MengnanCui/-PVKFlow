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
from typing import Any, Iterable, Iterator

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

        try:
            data = resp.json()
        except ValueError as exc:
            # 网关返回的不是 JSON。公司网络上这几乎总是同一件事：
            # 代理/SSO 把请求截下来，回了一个登录页或错误页，状态码还是 200。
            # 直接把 JSONDecodeError 抛上去的话，用户看到的是
            # 「Expecting value: line 1 column 1」—— 什么信息都没有。
            body = resp.text.strip()[:200].replace("\n", " ")
            raise ProviderUnavailable(
                f"{self.name} 返回的不是 JSON（可能是代理或登录页拦截了请求）："
                f"{body or '空响应'}") from exc
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable(f"{self.name} 返回结构异常：{str(data)[:300]}") from exc

        return ChatResult(text=text, model=payload["model"], provider=self.name,
                          usage=data.get("usage") or {}, raw=data)

    def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Iterator[str]:
        """逐块吐字。和 `chat` 走同一个 payload，只是 `stream: True`。

        本地 27B 一次回答动辄十几秒。不流式的话界面只能干转圈，
        用户不知道它是在想还是已经死了。
        """
        if not self.available():
            raise ProviderUnavailable(f"provider「{self.name}」配置不完整（缺 baseUrl 或 models）")

        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._normalize(messages),
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream("POST", url, headers=self._headers(),
                                   json=payload) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        raise ProviderUnavailable(
                            f"{self.name} 返回 {resp.status_code}：{resp.text[:400]}")
                    for piece in _iter_sse_text(resp.iter_lines()):
                        yield piece
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"连接 {self.name} 失败：{exc}") from exc


def _iter_sse_text(lines: Iterable[str]) -> Iterator[str]:
    """把 OpenAI 的 SSE 行流切成一段段正文。

    单独成函数是为了能拿假的行流直接测 —— 网关的实现千奇百怪，
    这里要能扛住的几种：`data:` 后有没有空格、心跳空行、
    结尾的 `[DONE]`、以及**半截 json**（少见但真的会有，
    崩在这里的话用户看到的是整个回答消失，而不是少几个字）。
    """
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":"):
            continue                       # 空行是分隔，`:` 开头是心跳注释
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue                       # 半截 json 就丢这一块，别把整条流带走
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece


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
    except Exception as exc:              # noqa: BLE001
        # 这个函数对界面的承诺是「失败也不抛」。它是用户排查连不上的**唯一**
        # 工具，自己 500 掉的话就什么都问不出来了。
        return {"ok": False, "provider": provider.name, "model": model_id,
                "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True, "provider": provider.name, "model": model_id,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "reply": r.text.strip()[:100],
    }


# ---------------------------------------------------------------- 模型发现
# 公共服务商的地址预设。**这里只放公开地址** —— 内网网关的地址和密钥一样，
# 属于用户自己的配置，只存在 workspace/config/providers.json（已 gitignore）。
# 界面上填过一次之后会一直回填，不需要写进仓库。
PRESETS = [
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
    {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"name": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1"},
    {"name": "本地 vLLM", "base_url": "http://127.0.0.1:8000/v1"},
    {"name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1"},
]


def list_remote_models(base_url: str, api_key: str = "",
                       timeout: float = 20.0) -> list[dict]:
    """`GET {base_url}/models` —— OpenAI 兼容协议的标准发现接口。

    有了它就不用手打模型名了。但**不是每个网关都实现它**，所以调用方必须
    保留「手动填」那条路：拉不到是常见情况，不是故障。

    返回 `[{"id": ..., "owned_by": ...}]`，按 id 排序。
    """
    base = (base_url or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ProviderUnavailable("接口地址要以 http:// 或 https:// 开头")

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(f"连不上 {base}：{exc}") from exc

    if resp.status_code == 404:
        raise ProviderUnavailable(
            f"{base}/models 返回 404 —— 这个网关没有提供模型列表接口。"
            "手动填模型名一样能用。")
    if resp.status_code in (401, 403):
        raise ProviderUnavailable(
            f"密钥被拒（HTTP {resp.status_code}）。检查一下 apiKey 是不是填错了。")
    if resp.status_code >= 400:
        raise ProviderUnavailable(f"{base}/models 返回 {resp.status_code}："
                                  f"{resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as exc:
        # 和 chat() 里同一件事：公司网络上代理/SSO 把请求截下来回了个登录页，
        # 状态码还是 200。不说清楚的话用户看到的就是「Expecting value」。
        body = resp.text.strip()[:200].replace("\n", " ")
        raise ProviderUnavailable(
            f"{base}/models 返回的不是 JSON（可能是代理或登录页拦截了请求）："
            f"{body or '空响应'}") from exc

    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ProviderUnavailable(f"返回结构不认识：{str(data)[:200]}")

    out = []
    for m in items:
        mid = (m.get("id") if isinstance(m, dict) else str(m)) or ""
        if mid:
            out.append({"id": mid, "owned_by": (m.get("owned_by") or "")
                        if isinstance(m, dict) else ""})
    return sorted(out, key=lambda x: x["id"])
