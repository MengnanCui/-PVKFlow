"""模型接入层。

原则：**没有配模型，平台也要完整可用。**
文件类型识别、skill 推荐、异常标记全部走确定性规则引擎（app/ai/rules.py），
模型只负责它真正擅长的部分：解释结果、写总结、从非结构化文本里抽字段。

界面上每条建议都标注来源是「规则」还是具体模型名，不制造假的智能感。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderUnavailable(RuntimeError):
    """没有可用模型。调用方应该退回规则引擎，而不是把错误抛给用户。"""


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
    name: str

    def available(self) -> bool: ...

    def chat(self, messages: list[ChatMessage], *, model: str | None = None,
             temperature: float = 0.2, max_tokens: int | None = None) -> ChatResult: ...


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
