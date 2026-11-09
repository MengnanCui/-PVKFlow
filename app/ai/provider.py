"""模型接入层。

原则：**没有配模型，平台也要完整可用。** 导入、解析、处理、批处理、对比、导出
一样都不依赖模型；模型只在 AI 抽屉和 ⓘ 提问里出现，没配时那里明说「还没配模型」。

（2026-11 之前还有一个「规则引擎」负责认文件、推荐 skill，随旧 Skill 机制一起退役了 ——
它服务的那两个接口早就没有界面入口。）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


class ProviderUnavailable(RuntimeError):
    """没有可用模型。调用方应该说清「先去设置里配模型」（接口回 501），而不是把原始错误抛给用户。"""


@dataclass
class ChatMessage:
    role: str            # system | user | assistant
    content: Any         # str，或 OpenAI 多模态的 content 数组

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResult:
    text: str
    model: str = ""
    provider: str = ""
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class AIProvider(Protocol):
    """一个模型服务要长成什么样（现在唯一的实现是 openai_compat.OpenAICompatProvider，两种协议都走它；
    请求的细节在 gateway.py）。要接一个完全不同的服务，实现这几个方法即可。"""
    name: str

    def available(self) -> bool: ...

    def chat(self, messages: list[ChatMessage], *, model: str | None = None,
             temperature: float = 0.2, max_tokens: int | None = None) -> ChatResult: ...

    def chat_stream(self, messages: list[ChatMessage], *, model: str | None = None,
                    temperature: float = 0.2, max_tokens: int | None = None) -> Iterator[str]: ...


class NullProvider:
    """没配模型时的占位。调用会抛 ProviderUnavailable，由调用方兜底。"""

    name = "none"

    def available(self) -> bool:
        return False

    def chat(self, messages, *, model=None, temperature=0.2, max_tokens=None) -> ChatResult:
        raise ProviderUnavailable("尚未配置模型。到「设置 → 模型」填入 providers 配置后即可使用。")


def extract_json(text: str) -> Any:
    """从模型回复里抠出 JSON。模型经常会包一层 ```json 或加几句解释。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 退而求其次：找第一个平衡的 {...} 或 [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(s)):
            if s[i] == opener:
                depth += 1
            elif s[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError("模型没有返回可解析的 JSON")
