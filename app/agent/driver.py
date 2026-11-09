"""模型驱动 —— 把「让模型决定下一步」这件事从循环里隔离出来（照 IFlow 的 lib/agent/driver.ts）。

网关支不支持 function calling，要真打一次才知道。两种情况下智能体都得能用：

  · toolcall —— 网关认 `tools` 参数：标准协议，模型直接给 tool_calls。
  · action   —— 网关只会普通对话：让模型输出一段约定格式的 JSON 动作，这边解析出来照样执行。
               能力几乎一样，只是更依赖提示词纪律。

默认 auto：先按 toolcall 发；网关明确拒绝 tools 参数（4xx 且报错里提到 tool / function）
就退到 action 重发一次，并记住这个网关 × 模型的脾气，后面不再白试。

只接 OpenAI 兼容协议 —— 和平台「设置 → 模型」里配的是同一份（同一个地址、密钥、模型名）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

import httpx

from app.ai.provider import ProviderUnavailable
from app.agent.tools import Tool

TIMEOUT = 180.0
# 查数据、审结果要的是稳，不是文采 —— 和 AI 抽屉的 0.3 一致
TEMPERATURE = 0.3

_learned: dict[str, str] = {}

# 测试用：换成带 MockTransport 的 client
client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=TIMEOUT)  # noqa: E731


@dataclass
class Call:
    id: str
    name: str
    args: dict


@dataclass
class Step:
    text: str = ""
    calls: list[Call] = field(default_factory=list)
    kind: str = "toolcall"
    model: str = ""


def reset_learned() -> None:
    _learned.clear()


# ------------------------------------------------------------------ 动作协议
def action_protocol(tools: list[Tool]) -> str:
    """action 模式追加的系统提示。「说完了」也是一个动作 —— 有了统一出口，不用猜这段话是结论还是想调工具。"""
    lines = [f"- {t.name}({', '.join(t.schema.get('properties', {}))}): {t.description}" for t in tools]
    return "\n".join([
        "【输出格式】你只能输出一个 JSON 对象，不要有任何别的文字，也不要用 ```。形状是：",
        '{"say": "给用户看的话（可以为空）", "calls": [{"name": "工具名", "args": {...}}]}',
        "calls 是空数组，表示你已经说完了，say 就是给用户的最终回答。",
        "需要查东西就把 calls 填上，结果会在下一轮给你，你可以接着再查。",
        "",
        "可用的工具：",
        *lines,
    ])


def extract_json(text: str):
    stripped = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(stripped)):
        if stripped[i] == "{":
            depth += 1
        elif stripped[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stripped[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


_seq = [0]


def _new_id() -> str:
    _seq[0] += 1
    return f"call_{_seq[0]}"


def parse_action(text: str) -> Step:
    j = extract_json(text)
    if not isinstance(j, dict):
        # 解不出结构 → 当成它在说话。宁可少调一次工具，也不要把一段正常回答丢掉
        return Step(text=text.strip(), kind="action")
    calls = [Call(_new_id(), c["name"], c.get("args") if isinstance(c.get("args"), dict) else {})
             for c in (j.get("calls") or []) if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"]]
    return Step(text=j.get("say") if isinstance(j.get("say"), str) else "", calls=calls, kind="action")


# ------------------------------------------------------------------ 请求
def to_openai(messages: list[dict], system: str) -> list[dict]:
    """内部消息 → OpenAI 的 messages。工具调用的参数要序列化成字符串。"""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["call_id"], "name": m["name"], "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("content") or None}
            if m.get("calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                                     for c in m["calls"]]
            out.append(msg)
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def to_action_messages(messages: list[dict], system: str) -> list[dict]:
    """action 模式下网关不认 tool 角色：工具结果改成一条 user 消息「工具结果：…」，调用改成它当时输出的 JSON。"""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "user", "content": f"【工具结果 {m['name']}（{m['call_id']}）】\n{m['content']}"})
        elif m["role"] == "assistant":
            if m.get("calls"):
                out.append({"role": "assistant", "content": json.dumps(
                    {"say": m.get("content") or "", "calls": [{"name": c["name"], "args": c["args"]} for c in m["calls"]]},
                    ensure_ascii=False)})
            else:
                out.append({"role": "assistant", "content": m.get("content") or ""})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _rejects_tools(status: int, body: str) -> bool:
    return status in (400, 404, 422) and bool(re.search(r"tool|function", body, re.I))


def step(provider, model: str, system: str, messages: list[dict], tools: list[Tool], *,
         mode: str = "auto", on_delta: Callable[[str], None] | None = None) -> Step:
    """问模型一次「下一步做什么」。流式收：正文逐段交给 on_delta（toolcall 模式下），工具调用拼完整再返回。"""
    key = f"{provider.base_url}::{model}"
    kind = mode if mode in ("toolcall", "action") else _learned.get(key, "toolcall")
    for _ in range(2):
        if kind == "toolcall":
            body = {"model": model, "temperature": TEMPERATURE, "stream": True,
                    "messages": to_openai(messages, system),
                    "tools": [{"type": "function", "function": {
                        "name": t.name, "description": t.description, "parameters": t.schema}} for t in tools],
                    "tool_choice": "auto"}
        else:
            body = {"model": model, "temperature": TEMPERATURE, "stream": True,
                    "messages": to_action_messages(messages, system + "\n\n" + action_protocol(tools))}
        url = f"{provider.base_url}/chat/completions"
        try:
            with client_factory() as client, client.stream("POST", url, headers=provider._headers(), json=body) as resp:
                if resp.status_code >= 400:
                    text = resp.read().decode("utf-8", "replace")
                    if kind == "toolcall" and mode == "auto" and _rejects_tools(resp.status_code, text):
                        _learned[key] = kind = "action"
                        continue
                    raise ProviderUnavailable(f"{provider.name} 返回 {resp.status_code}：{text[:300]}")
                ctype = resp.headers.get("content-type", "")
                if "event-stream" in ctype:
                    content, calls = _read_stream(resp.iter_lines(), on_delta if kind == "toolcall" else None)
                else:                   # 要了流式却回了整包：照样能用
                    content, calls = _read_whole(resp.read().decode("utf-8", "replace"))
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"连接 {provider.name} 失败：{exc}") from exc
        if mode == "auto":
            _learned[key] = kind
        if kind == "action":
            s = parse_action(content)
            s.model = model
            return s
        return Step(text=content, calls=calls, kind=kind, model=model)
    raise ProviderUnavailable("网关既不接受工具调用，退回动作协议也没成功")


def _parse_args(raw: str) -> dict:
    # arguments 是字符串，模型偶尔给出不合法的 JSON —— 解不出来当空参数，
    # 让下游的校验去报「缺少必填参数」：那句话模型看得懂、改得动
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _read_stream(lines: Iterable[str], on_delta) -> tuple[str, list[Call]]:
    """SSE → (正文, 工具调用)。工具调用是按 index 一截一截来的（名字先到，参数分好几块），要拼起来。"""
    text: list[str] = []
    parts: dict[int, dict] = {}
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or ch.get("message") or {}
            if d.get("content"):
                text.append(d["content"])
                if on_delta:
                    on_delta(d["content"])
            for tc in d.get("tool_calls") or []:
                p = parts.setdefault(int(tc.get("index", len(parts))), {"id": "", "name": "", "args": ""})
                p["id"] = tc.get("id") or p["id"]
                fn = tc.get("function") or {}
                p["name"] += fn.get("name") or ""
                p["args"] += fn.get("arguments") or ""
    calls = [Call(p["id"] or _new_id(), p["name"], _parse_args(p["args"]))
             for _, p in sorted(parts.items()) if p["name"]]
    return "".join(text), calls


def _read_whole(body: str) -> tuple[str, list[Call]]:
    try:
        msg = json.loads(body)["choices"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ProviderUnavailable(f"网关返回的不是预期的结构：{body[:200]}") from exc
    calls = [Call(c.get("id") or _new_id(), (c.get("function") or {}).get("name", ""),
                  _parse_args((c.get("function") or {}).get("arguments", "")))
             for c in msg.get("tool_calls") or []]
    return msg.get("content") or "", [c for c in calls if c.name]
