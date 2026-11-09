"""模型网关 —— 照 IFlow 的 lib/server/llm.ts、llmChat.ts、thinking.ts、llmProbe.ts、agent/stream.ts 改写。

各家协议的差异都在这里消化，上面（AI 助手、智能体、设置页的测试）只认一个接口：

  · 两种协议：OpenAI 兼容（`/chat/completions`）和 Anthropic（`/v1/messages`）。
    providers.json 里每个 provider 的 `api` 字段决定走哪种：`openai-completions` / `anthropic-messages`
  · 带退避的重试：429 / 502 / 503 / 504 按 1 s、2 s、4 s 重试三次（认 Retry-After，最多等 30 s）；
    500 不重试 —— 多半是请求本身的问题，重试只会把失败拖长
  · 思考参数：各家写法完全不统一（DeepSeek-V4 / Kimi-K2 认 chat_template_kwargs.thinking，GLM-5 认
    enable_thinking，Anthropic 是 thinking.type……），按模型 id 认出是哪一派再塞；
    **缺省显式关思考**（IFlow 在内网网关上实测：开着思考会把输出吞掉或挂住）
  · 网关拒收某个可选参数（思考 / 流式）就摘掉重试一次，并记住这个网关 × 模型，后面不再白试
  · 拉模型列表、逐个模型打一句测延迟（同一家族只留最新版本）
  · 可选的主机白名单：环境变量 HTE_LLM_ALLOWED_HOSTS（逗号分隔），不设不限
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlparse

import httpx

from app.ai.provider import ProviderUnavailable

OPENAI, ANTHROPIC = "openai-completions", "anthropic-messages"
PROTOCOLS = {OPENAI: "OpenAI 兼容", ANTHROPIC: "Anthropic"}
ANTHROPIC_VERSION = "2023-06-01"

TIMEOUT = float(os.environ.get("HTE_LLM_TIMEOUT", "120"))
RETRY_STATUS = {429, 502, 503, 504}
RETRY_MAX = max(0, int(os.environ.get("HTE_LLM_RETRIES", "3")))
RETRY_BASE = float(os.environ.get("HTE_LLM_RETRY_BASE", "1.0"))
RETRY_CAP = 30.0

# 测试里换成不睡的版本；client_factory 换成带 MockTransport 的
sleep: Callable[[float], None] = time.sleep
client_factory: Callable[[float], httpx.Client] = lambda timeout=TIMEOUT: httpx.Client(timeout=timeout)  # noqa: E731

# 「这个网关 × 模型拒收过 X」—— 进程内记住，重启后重新试
_no_think: set[str] = set()
_no_stream: set[str] = set()


def reset_memo() -> None:
    _no_think.clear()
    _no_stream.clear()


def protocol_of(cfg: dict) -> str:
    return ANTHROPIC if (cfg.get("api") or "").startswith("anthropic") else OPENAI


# ------------------------------------------------------------------ 地址与鉴权
def check_url(raw: str) -> str:
    """只放行 http / https；设了 HTE_LLM_ALLOWED_HOSTS 时还要在白名单里。"""
    u = urlparse(raw or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ProviderUnavailable("接口地址要以 http:// 或 https:// 开头")
    allow = [h.strip().lower() for h in os.environ.get("HTE_LLM_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allow and u.hostname.lower() not in allow:
        raise ProviderUnavailable(f"网关主机不在允许列表里：{u.hostname}（HTE_LLM_ALLOWED_HOSTS）")
    return raw


def headers(protocol: str, api_key: str = "") -> dict:
    h = {"Content-Type": "application/json"}
    if protocol == ANTHROPIC:
        h["anthropic-version"] = ANTHROPIC_VERSION
        if api_key:
            h["x-api-key"] = api_key
    elif api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _anthropic_base(base: str) -> str:
    """Anthropic 的地址有人写 https://api.anthropic.com，有人写到 /v1 —— 两种都认。"""
    base = base.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def chat_url(base: str, protocol: str) -> str:
    return (_anthropic_base(base) + "/v1/messages") if protocol == ANTHROPIC else base.rstrip("/") + "/chat/completions"


def models_url(base: str, protocol: str) -> str:
    return (_anthropic_base(base) + "/v1/models") if protocol == ANTHROPIC else base.rstrip("/") + "/models"


# ------------------------------------------------------------------ 带退避的请求
def _retry_after(resp: httpx.Response, i: int) -> float:
    try:
        ra = float(resp.headers.get("retry-after", ""))
        if ra > 0:
            return min(ra, RETRY_CAP)
    except ValueError:
        pass
    return min(RETRY_BASE * (2 ** i), RETRY_CAP)


def send(client: httpx.Client, method: str, url: str, *, headers: dict, json_body: Any = None,
         stream: bool = False) -> httpx.Response:
    """发请求；429 / 502 / 503 / 504 退避重试。用尽了把最后一次响应原样交回去（不吞），调用方按状态码处理。
    stream=True 时返回的响应要调用方关。"""
    check_url(url)
    try:
        for i in range(RETRY_MAX + 1):
            req = client.build_request(method, url, headers=headers, json=json_body)
            resp = client.send(req, stream=stream)
            if resp.status_code not in RETRY_STATUS or i == RETRY_MAX:
                return resp
            wait = _retry_after(resp, i)
            resp.close()
            sleep(wait)
    except httpx.TimeoutException as exc:
        raise ProviderUnavailable(f"网关没有响应（超过 {client.timeout.read or TIMEOUT:.0f} 秒）") from exc
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(f"连不上网关：{exc}") from exc
    raise AssertionError("unreachable")


# ------------------------------------------------------------------ 思考参数
def thinking_kind(model: str, protocol: str) -> str:
    """认模型属于哪一派（照 IFlow thinking.ts）。认错了不报错、只会静默不生效，所以有测试兜着。"""
    m = (model or "").lower()
    if protocol == ANTHROPIC or "claude" in m:
        return "anthropic"
    if re.search(r"deepseek-(reasoner|r1)|^o[1-9](-|$)|gpt-5-thinking", m):
        return "always"                      # 恒定思考、不吃参数：发了反而可能报错
    if re.search(r"^(o[1-9]|gpt-5|gpt-4\.1)", m) or "reasoning" in m:
        return "openai"
    if re.search(r"qwen3|qwq|qwen-?plus-?latest", m):
        return "qwen"
    if re.search(r"glm-?5", m):
        return "k2"
    if re.search(r"deepseek-?v4|kimi-?k2", m):
        return "k1"
    if re.search(r"glm-4\.[5-9]|glm-z", m):
        return "glm"
    return "none"


THINK_BUDGET = 4096


def thinking_fields(model: str, protocol: str, choice: str) -> dict:
    """该并进请求体的思考字段。choice：off（缺省，显式关）/ on / default（不发）。认不出写法 → 空。"""
    if choice not in ("on", "off"):
        return {}
    on = choice == "on"
    kind = thinking_kind(model, protocol)
    if kind == "anthropic":
        return {"thinking": {"type": "enabled", "budget_tokens": THINK_BUDGET}} if on else {}
    if kind == "openai":
        return {"reasoning_effort": "medium"} if on else {}      # 关不掉，只能不发
    if kind == "qwen":
        return {"chat_template_kwargs": {"enable_thinking": on}}
    if kind == "k1":
        return {"chat_template_kwargs": {"thinking": on}}
    if kind == "k2":
        return {"chat_template_kwargs": {"enable_thinking": on}}
    if kind == "glm":
        return {"thinking": {"type": "enabled" if on else "disabled"}}
    return {}


_THINK_KEYS = ("thinking", "reasoning_effort", "chat_template_kwargs")


# ------------------------------------------------------------------ 消息格式
def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    sys = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "system")
    return sys, [m for m in messages if m["role"] != "system"]


def merge_turns(messages: list[dict]) -> list[dict]:
    """Anthropic 要求 user / assistant 交替：相邻同角色的并成一条（内容转成块列表）。"""
    out: list[dict] = []
    for m in messages:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": str(m["content"])}]
        blocks = [b for b in blocks if not (b.get("type") == "text" and not b.get("text"))] or [{"type": "text", "text": "（空）"}]
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": m["role"], "content": list(blocks)})
    return out


def build_body(protocol: str, model: str, messages: list[dict], *, temperature: float | None,
               max_tokens: int | None, stream: bool, thinking: dict) -> dict:
    """普通对话（不带工具）的请求体。messages 是 [{role, content}]，role 可以是 system。"""
    if protocol == ANTHROPIC:
        sys, rest = split_system(messages)
        body: dict = {"model": model, "max_tokens": max_tokens or 4096, "messages": merge_turns(rest)}
        if sys:
            body["system"] = sys
        if temperature is not None:
            body["temperature"] = temperature
    else:
        body = {"model": model, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_tokens"] = max_tokens
    if stream:
        body["stream"] = True
    return finish_body(body, protocol, thinking)


def finish_body(body: dict, protocol: str, thinking: dict) -> dict:
    """并进思考字段。Anthropic 开思考时 max_tokens 必须大于预算、且不能同时给 temperature。"""
    body.update(json.loads(json.dumps(thinking)))
    t = body.get("thinking")
    if protocol == ANTHROPIC and isinstance(t, dict) and t.get("type") == "enabled":
        body.pop("temperature", None)
        if body.get("max_tokens", 0) <= t["budget_tokens"]:
            body["max_tokens"] = t["budget_tokens"] + 4096
    return body


# ------------------------------------------------------------------ 发一次（带降级）
def request(provider, body: dict, *, stream: bool, timeout: float = TIMEOUT):
    """发一次对话请求，网关拒收可选参数时摘掉重试。返回 (client, response, streamed)，调用方负责关两样。

    降级顺序照 IFlow：先摘思考参数，再退回整包（不流式）。每一样都记住这个网关 × 模型。
    """
    model = body.get("model", "")
    key = f"{provider.base_url}::{model}"
    if key in _no_think:
        for k in _THINK_KEYS:
            body.pop(k, None)
    want_stream = stream and key not in _no_stream
    if not want_stream:
        body.pop("stream", None)
        body.pop("stream_options", None)
    url = chat_url(provider.base_url, provider.protocol)
    hdr = headers(provider.protocol, provider.api_key)
    client = client_factory(timeout)
    try:
        for _ in range(3):
            resp = send(client, "POST", url, headers=hdr, json_body=body, stream=True)
            if resp.status_code < 400:
                ctype = resp.headers.get("content-type", "")
                streamed = want_stream and "event-stream" in ctype
                if want_stream and not streamed:
                    _no_stream.add(key)          # 说支持却回了整包 —— 下次别要流式了
                return client, resp, streamed
            text = resp.read().decode("utf-8", "replace")
            resp.close()
            if "tools" in body and re.search(r"tool|function", text, re.I):
                # 网关不认工具调用：交给智能体的驱动退到动作协议，这里别误把思考参数记成「被拒」
                raise GatewayError(resp.status_code, text, provider.name)
            if resp.status_code in (400, 422):
                if any(k in body for k in _THINK_KEYS):
                    _no_think.add(key)
                    for k in _THINK_KEYS:
                        body.pop(k, None)
                    continue
                if want_stream and not re.search(r"tool|function", text, re.I):
                    _no_stream.add(key)
                    want_stream = False
                    body.pop("stream", None)
                    body.pop("stream_options", None)
                    continue
            raise GatewayError(resp.status_code, text, provider.name)
        raise GatewayError(400, "网关一再拒收这次请求", provider.name)
    except BaseException:
        client.close()
        raise


class GatewayError(ProviderUnavailable):
    def __init__(self, status: int, body: str, name: str = "网关"):
        self.status = status
        self.body = body
        super().__init__(f"{name} 返回 {status}：{body[:300]}")


# ------------------------------------------------------------------ 解析
def parse_whole(data: dict, protocol: str) -> tuple[str, list[dict], dict]:
    """整包响应 → (正文, 工具调用 [{id, name, arguments(str)}], usage)。"""
    if protocol == ANTHROPIC:
        parts = data.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        calls = [{"id": p.get("id") or "", "name": p.get("name") or "",
                  "arguments": json.dumps(p.get("input") or {}, ensure_ascii=False)}
                 for p in parts if p.get("type") == "tool_use"]
        return text, calls, data.get("usage") or {}
    msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    calls = [{"id": c.get("id") or "", "name": (c.get("function") or {}).get("name") or "",
              "arguments": (c.get("function") or {}).get("arguments") or ""}
             for c in msg.get("tool_calls") or []]
    return msg.get("content") or "", calls, data.get("usage") or {}


def read_stream(lines: Iterable[str], protocol: str,
                on_text: Callable[[str], None] | None = None) -> tuple[str, list[dict]]:
    """SSE → (正文, 工具调用)。工具调用是一截一截来的（OpenAI 按 index，Anthropic 按 content block），
    **接着拼，不是覆盖** —— 拼错了不报错，只会让参数变成半截 JSON。思考内容不进正文。"""
    text: list[str] = []
    parts: dict[int, dict] = {}
    event = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue                              # 心跳 / 半截：丢这一块，别把整条流带走
        if isinstance(obj.get("error"), dict) and obj["error"].get("message"):
            raise ProviderUnavailable(f"网关流式返回错误：{obj['error']['message']}")
        if protocol == ANTHROPIC:
            typ = obj.get("type") or event
            if typ == "content_block_start":
                cb = obj.get("content_block") or {}
                parts[int(obj.get("index", len(parts)))] = {
                    "type": cb.get("type", "text"), "id": cb.get("id") or "", "name": cb.get("name") or "", "args": ""}
            elif typ == "content_block_delta":
                d = obj.get("delta") or {}
                p = parts.setdefault(int(obj.get("index", 0)), {"type": "text", "id": "", "name": "", "args": ""})
                if d.get("type") == "text_delta" and d.get("text"):
                    text.append(d["text"])
                    if on_text:
                        on_text(d["text"])
                elif d.get("type") == "input_json_delta":
                    p["args"] += d.get("partial_json") or ""
            elif typ == "message_stop":
                break
            continue
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or ch.get("message") or {}
            if d.get("content"):
                text.append(d["content"])
                if on_text:
                    on_text(d["content"])
            for tc in d.get("tool_calls") or []:
                p = parts.setdefault(int(tc.get("index", len(parts))), {"type": "tool_use", "id": "", "name": "", "args": ""})
                p["id"] = tc.get("id") or p["id"]
                fn = tc.get("function") or {}
                p["name"] += fn.get("name") or ""
                p["args"] += fn.get("arguments") or ""
    calls = [{"id": p["id"], "name": p["name"], "arguments": p["args"]}
             for _, p in sorted(parts.items()) if p["type"] == "tool_use" and p["name"]]
    return "".join(text), calls


def response_text(resp: httpx.Response, streamed: bool, protocol: str,
                  on_text: Callable[[str], None] | None = None) -> tuple[str, list[dict]]:
    if streamed:
        return read_stream(resp.iter_lines(), protocol, on_text)
    raw = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # 公司网络上这几乎总是同一件事：代理 / SSO 把请求截下来回了个登录页，状态码还是 200
        body = raw.strip()[:200].replace("\n", " ")
        raise ProviderUnavailable(f"网关返回的不是 JSON（可能是代理或登录页拦截了请求）：{body or '空响应'}") from exc
    text, calls, _ = parse_whole(data, protocol)
    if on_text and text:
        on_text(text)
    return text, calls


# ------------------------------------------------------------------ 普通对话
def chat(provider, messages: list[dict], *, model: str, temperature: float | None = 0.2,
         max_tokens: int | None = None, timeout: float = TIMEOUT) -> tuple[str, dict]:
    body = build_body(provider.protocol, model, messages, temperature=temperature, max_tokens=max_tokens,
                      stream=False, thinking=thinking_fields(model, provider.protocol, provider.thinking))
    client, resp, _ = request(provider, body, stream=False, timeout=timeout)
    try:
        raw = resp.read().decode("utf-8", "replace")
    finally:
        resp.close()
        client.close()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        body_txt = raw.strip()[:200].replace("\n", " ")
        raise ProviderUnavailable(
            f"{provider.name} 返回的不是 JSON（可能是代理或登录页拦截了请求）：{body_txt or '空响应'}") from exc
    text, _, usage = parse_whole(data, provider.protocol)
    if not text:
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        if msg.get("reasoning_content") or msg.get("reasoning"):
            raise ProviderUnavailable(f"{provider.name} 只回了思考内容、正文是空的 —— 在设置里把这个模型的「思考」关掉")
        if "choices" not in data and "content" not in data:
            raise ProviderUnavailable(f"{provider.name} 返回结构异常：{str(data)[:300]}")
    return text, usage


def chat_stream(provider, messages: list[dict], *, model: str, temperature: float | None = 0.2,
                max_tokens: int | None = None, timeout: float = TIMEOUT) -> Iterator[str]:
    """逐段吐正文。网关不认流式就退回整包，一次吐完 —— 功能不减，只是没有逐字效果。"""
    body = build_body(provider.protocol, model, messages, temperature=temperature, max_tokens=max_tokens,
                      stream=True, thinking=thinking_fields(model, provider.protocol, provider.thinking))
    client, resp, streamed = request(provider, body, stream=True, timeout=timeout)
    try:
        if not streamed:
            text, _ = response_text(resp, False, provider.protocol)
            if text:
                yield text
            return
        for piece in _iter_text(resp.iter_lines(), provider.protocol):
            yield piece
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(f"连接 {provider.name} 中断：{exc}") from exc
    finally:
        resp.close()
        client.close()


def _iter_text(lines: Iterable[str], protocol: str) -> Iterator[str]:
    """边收边吐正文（不等整条流结束）。和 read_stream 同一套规矩，只是不收工具调用。"""
    event = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj.get("error"), dict) and obj["error"].get("message"):
            raise ProviderUnavailable(f"网关流式返回错误：{obj['error']['message']}")
        if protocol == ANTHROPIC:
            typ = obj.get("type") or event
            if typ == "content_block_delta":
                d = obj.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    yield d["text"]
            elif typ == "message_stop":
                return
            continue
        for ch in obj.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                yield piece


# ------------------------------------------------------------------ 拉列表、测速
def list_models(base_url: str, api_key: str, protocol: str, timeout: float = 20.0) -> list[dict]:
    base = (base_url or "").rstrip("/")
    check_url(base)
    with client_factory(timeout) as client:
        resp = send(client, "GET", models_url(base, protocol), headers=headers(protocol, api_key))
        body = resp.text
    if resp.status_code == 404:
        raise ProviderUnavailable(f"{models_url(base, protocol)} 返回 404 —— 这个网关没有提供模型列表接口。手动填模型名一样能用。")
    if resp.status_code in (401, 403):
        raise ProviderUnavailable(f"密钥被拒（HTTP {resp.status_code}）。检查一下 apiKey 是不是填错了。")
    if resp.status_code >= 400:
        raise ProviderUnavailable(f"拉模型列表返回 {resp.status_code}：{body[:300]}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderUnavailable(f"模型列表返回的不是 JSON（可能是代理或登录页拦截了请求）：{body.strip()[:200]}") from exc
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ProviderUnavailable(f"返回结构不认识：{str(data)[:200]}")
    out = []
    for m in items:
        mid = (m.get("id") if isinstance(m, dict) else str(m)) or ""
        if mid:
            out.append({"id": mid, "owned_by": (m.get("owned_by") or "") if isinstance(m, dict) else ""})
    return sorted(out, key=lambda x: x["id"])


def family_of(model_id: str) -> tuple[str, tuple[int, ...]]:
    """版本数字 → #，得到家族键；版本本身转成可比较的元组（照 IFlow llmProbe.familyOf）。"""
    version: list[int] = []

    def take(m):
        version.extend(int(x) for x in m.group(0).split("."))
        return "#"
    fam = re.sub(r"\d+(?:\.\d+)*", take, model_id.lower())
    return re.sub(r"[-_ ]+", "-", fam), tuple(version)


def dedupe_models(ids: list[str]) -> tuple[list[str], list[dict]]:
    """同一家族只留最新版本（Kimi-K2.5 / K2.6 → 留 K2.6）；flash / pro 这类后缀算不同家族，各自保留。"""
    best: dict[str, tuple[str, tuple[int, ...]]] = {}
    for i in ids:
        fam, ver = family_of(i)
        if fam not in best or ver > best[fam][1]:
            best[fam] = (i, ver)
    kept = {v[0] for v in best.values()}
    return ([i for i in ids if i in kept],
            [{"id": i, "by": best[family_of(i)[0]][0]} for i in ids if i not in kept])


PROBE_TIMEOUT = 20.0
# 模型名的合法形状（照 IFlow llmChat.MODEL_RE）—— 测速接口只认这种，别把任意字符串往网关送
MODEL_RE = re.compile(r"^[\w.\-:/]{1,120}$")


def ping(base_url: str, api_key: str, protocol: str, model: str) -> dict:
    """给一个模型打一句「回复 ok」，回延迟。**不抛**：测不通也是一条结果，列表要把它沉底。"""
    t0 = time.perf_counter()
    base = (base_url or "").rstrip("/")
    body = ({"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "回复 ok"}]}
            if protocol == ANTHROPIC else
            {"model": model, "max_tokens": 8, "temperature": 0, "messages": [{"role": "user", "content": "回复 ok"}]})
    try:
        with client_factory(PROBE_TIMEOUT) as client:
            resp = send(client, "POST", chat_url(base, protocol), headers=headers(protocol, api_key), json_body=body)
            raw = resp.text
        ms = round((time.perf_counter() - t0) * 1000)
        if resp.status_code >= 400:
            return {"id": model, "ok": False, "ms": ms, "error": f"HTTP {resp.status_code}: {raw[:120]}"}
        text, _, _ = parse_whole(json.loads(raw), protocol)
        return {"id": model, "ok": True, "ms": ms, "reply": text.strip()[:40]}
    except ProviderUnavailable as exc:
        return {"id": model, "ok": False, "ms": round((time.perf_counter() - t0) * 1000), "error": str(exc)[:160]}
    except (ValueError, KeyError, TypeError) as exc:
        return {"id": model, "ok": False, "ms": round((time.perf_counter() - t0) * 1000),
                "error": f"返回结构不认识：{exc}"[:160]}
