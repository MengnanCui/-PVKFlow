"""领域层抛给接口层的异常（接口层 `app/api/common.guard` 负责翻成 HTTP 错误）。

- **NotFound** = 用户要的东西不存在（样品、任务、处理、模块……）→ 404
- **Invalid**  = 用户给的东西不对（参数、时间窗、列名……）→ 400

两个都可以带 `kind`（给前端区分是哪一种错，比如 `no_thickness`、`bad_window`），默认 `not_found` / `invalid`。
NotFound 是 KeyError 的子类、Invalid 是 ValueError 的子类，老代码里的 `except KeyError / ValueError` 照样接得住；
但**普通的 KeyError 不再被当成 404** —— 那多半是程序自己的 bug（拿错了 dict 的键），原来会显示成「没有这个：xxx」，
把 bug 藏住了（C073）。领域层不要 import `app.api`，也不要抛 ApiError。
"""
from __future__ import annotations


class NotFound(KeyError):
    def __init__(self, message: str = "没有找到", kind: str = "not_found") -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:          # KeyError 的 str() 会给消息加引号；这里要原样的人话
        return str(self.args[0]) if self.args else "没有找到"


class Invalid(ValueError):
    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind
