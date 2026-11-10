"""流式响应（SSE）的共用部分：AI 抽屉（app/api/chat.py）和智能体（app/api/agent.py）都用。

原来是 chat.py 里的私有函数，智能体那边跨文件 import 私有名字（C074 抽出来）。
"""
from __future__ import annotations

import json

import anyio
from fastapi.responses import StreamingResponse

from app.ai import openai_compat
from app.ai.provider import ProviderUnavailable
from app.api.common import ApiError


def sse_frame(event: str, data: dict) -> bytes:
    """一帧 SSE：`event: 名字` + `data: json`。"""
    return (f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")


def resolve_or_501(provider_name: str | None, model: str | None):
    """找一个可用的模型；没有就 501。**必须在开流之前调**：流一开始，状态码就已经发出去了。"""
    try:
        return openai_compat.resolve(provider_name, model)
    except ProviderUnavailable as exc:
        raise ApiError(str(exc), 501, "no_model") from exc


class ClosingStream(StreamingResponse):
    """流结束（不管是说完了、还是你点了停止 / 关了页面）时**亲手**把生成器关掉。

    Starlette 发现客户端断开后只是不再往下取，并不关生成器 —— 生成器停在 yield 上，
    它的 finally（存下已经说出来的字）要等垃圾回收哪天碰巧跑到才执行。实测断开 4 秒后
    消息仍是空白的「生成中」（自查 §6 #16）。
    """

    def __init__(self, gen, **kw):
        self._gen = gen
        super().__init__(gen, **kw)

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # 关生成器会写库、关模型那头的连接：放到线程里做；并且不许被这次请求的取消打断，
            # 不然断开时恰恰最需要跑的这一步会被跳过
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._close)

    def _close(self) -> None:
        try:
            self._gen.close()
        except ValueError:          # 极少数情况下它正在别的线程里跑着；那就让它自己跑完
            pass
