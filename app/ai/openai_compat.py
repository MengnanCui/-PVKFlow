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
from typing import Iterable, Iterator


from app import config
from app.ai import gateway
from app.ai.provider import ChatMessage, ChatResult, ProviderUnavailable

DEFAULT_TIMEOUT = 120.0
THINKING = ("off", "on", "default")


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
            "thinking": pcfg.get("thinking") or "off",
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
    """一个 provider 条目对应一个实例。

    名字是历史留下的：现在它也接 Anthropic 协议（`api: anthropic-messages`）。
    网络那一层（两种协议、退避重试、思考参数、拒收降级）全在 app/ai/gateway.py。
    """

    def __init__(self, name: str, cfg: dict) -> None:
        self.name = name
        self.base_url = (cfg.get("baseUrl") or "").rstrip("/")
        self.api_key = cfg.get("apiKey") or ""
        self.compat = cfg.get("compat") or {}
        self.protocol = gateway.protocol_of(cfg)
        # 思考：off（缺省，显式关 —— IFlow 在内网网关上实测开着会吞输出）/ on / default（不发参数）
        t = cfg.get("thinking") or "off"
        self.thinking = t if t in THINKING else "off"
        self.models = [m.get("id") for m in (cfg.get("models") or []) if m.get("id")]
        self.default_model = self.models[0] if self.models else None

    def available(self) -> bool:
        return bool(self.base_url and self.default_model)

    def _headers(self) -> dict:
        return gateway.headers(self.protocol, self.api_key)

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
        mid = model or self.default_model
        text, usage = gateway.chat(self, self._normalize(messages), model=mid, temperature=temperature,
                                   max_tokens=max_tokens, timeout=timeout)
        return ChatResult(text=text, model=mid, provider=self.name, usage=usage)

    def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Iterator[str]:
        """逐块吐字。本地 27B 一次回答动辄十几秒 —— 不流式的话界面只能干转圈。"""
        if not self.available():
            raise ProviderUnavailable(f"provider「{self.name}」配置不完整（缺 baseUrl 或 models）")
        yield from gateway.chat_stream(self, self._normalize(messages), model=model or self.default_model,
                                       temperature=temperature, max_tokens=max_tokens, timeout=timeout)


def _iter_sse_text(lines: Iterable[str]) -> Iterator[str]:
    """OpenAI 的 SSE 行流 → 一段段正文。实现在 gateway（两种协议同一套规矩），这里留个老名字。"""
    return gateway._iter_text(lines, gateway.OPENAI)


def resolve(provider_name: str | None = None, model: str | None = None):
    """按「设置里选的 → 配置里第一个」的顺序找一个可用 provider。

    返回 (provider, model_id)。找不到就抛 ProviderUnavailable，
    调用方负责说清「没配模型」（接口回 501）。
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
# 地址预设（照 IFlow 的 lib/llm/config.ts）：带协议、模型提示、要不要密钥。
# 第一条是**内设的内网网关**（P-50：你要求加进来，和 IFlow 同一个地址）—— 只放地址，
# **密钥永远不进仓库**，各人在设置页填自己的。其余是公共服务商和本机地址。
PRESETS = [
    {"name": "内网 NewAPI 网关（内设）", "base_url": "http://newapi.catl.com/v1", "api": "openai-completions",
     "model_hint": "", "internal": True,
     "note": "公司内网网关，密钥向管理员要；填好后点「拉取并测速」挑模型"},
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "api": "openai-completions",
     "model_hint": "deepseek-chat"},
    {"name": "Qwen · 通义（DashScope）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "api": "openai-completions", "model_hint": "qwen-plus"},
    {"name": "Moonshot · Kimi", "base_url": "https://api.moonshot.cn/v1", "api": "openai-completions",
     "model_hint": "moonshot-v1-8k"},
    {"name": "GLM · 智谱", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api": "openai-completions",
     "model_hint": "glm-4-flash"},
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "api": "openai-completions",
     "model_hint": "gpt-4o-mini"},
    {"name": "Anthropic · Claude", "base_url": "https://api.anthropic.com", "api": "anthropic-messages",
     "model_hint": "claude-sonnet-4-5"},
    {"name": "本地 vLLM", "base_url": "http://127.0.0.1:8000/v1", "api": "openai-completions",
     "model_hint": "", "needs_key": False},
    {"name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1", "api": "openai-completions",
     "model_hint": "qwen2.5:7b", "needs_key": False},
]

# 一份本机名单最多存这么多条。这道闸挡的是「点错了按钮攒出几百条」，
# 不是功能限制 —— 一个人手上的网关不会有二十个。
MAX_USER_PRESETS = 20


def user_presets(path: Path | None = None) -> list[dict]:
    """用户自己存下来的地址预设。

    存在 workspace/config/providers.json 的 `presets` 里 —— 和密钥同一个文件、
    同一条 .gitignore。**内网地址不进仓库**，仓库里那份 PRESETS 只有公共网关。
    """
    try:
        cfg = load_config(path)
    except ProviderUnavailable:
        return []
    out = []
    for item in (cfg.get("presets") or []):
        if not isinstance(item, dict):
            continue
        url = (item.get("base_url") or "").strip().rstrip("/")
        if url:
            out.append({"name": (item.get("name") or url).strip()[:60],
                        "base_url": url, "origin": "user",
                        "api": item.get("api") or gateway.OPENAI})
    return out[:MAX_USER_PRESETS]


def all_presets(path: Path | None = None) -> list[dict]:
    """下拉里那份候选地址：**自己存的排在前面**，内置公共网关跟在后面。

    自己存的排前面，是因为那才是你每天要用的那个；
    公共的留在后面，第一次打开设置页的人还是有东西可选。
    地址相同的算同一条，用户那份的名字胜出。
    """
    out, seen = [], set()
    for item in user_presets(path) + [{**p, "origin": "builtin"} for p in PRESETS]:
        key = item["base_url"].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def save_preset(name: str, base_url: str, path: Path | None = None, api: str = "openai-completions") -> list[dict]:
    """把一个地址存进本机名单。地址相同就换个名字，不新增一条。"""
    url = (base_url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("接口地址要以 http:// 或 https:// 开头")
    name = (name or url).strip()[:60]

    try:
        cfg = load_config(path)
    except ProviderUnavailable:
        cfg = {"providers": {}}
    keep = [x for x in (cfg.get("presets") or [])
            if isinstance(x, dict)
            and (x.get("base_url") or "").rstrip("/") != url]
    entry = {"name": name, "base_url": url}
    if api == gateway.ANTHROPIC:
        entry["api"] = api
    cfg["presets"] = ([entry] + keep)[:MAX_USER_PRESETS]
    save_config(cfg, path)
    return user_presets(path)


def delete_preset(base_url: str, path: Path | None = None) -> list[dict]:
    """从本机名单里去掉一个地址。内置的那几条去不掉 —— 它们不在这个文件里。"""
    url = (base_url or "").strip().rstrip("/")
    try:
        cfg = load_config(path)
    except ProviderUnavailable:
        return []
    cfg["presets"] = [x for x in (cfg.get("presets") or [])
                      if isinstance(x, dict)
                      and (x.get("base_url") or "").rstrip("/") != url]
    save_config(cfg, path)
    return user_presets(path)


def list_remote_models(base_url: str, api_key: str = "", timeout: float = 20.0,
                       api: str = "openai-completions") -> list[dict]:
    """拉网关的模型列表（OpenAI 兼容是 `GET {base}/models`，Anthropic 是 `/v1/models`）。

    有了它就不用手打模型名了。但**不是每个网关都实现它**，所以调用方必须保留「手动填」那条路：
    拉不到是常见情况，不是故障。返回 `[{"id": ..., "owned_by": ...}]`，按 id 排序。
    """
    return gateway.list_models(base_url, api_key, gateway.protocol_of({"api": api}), timeout)
