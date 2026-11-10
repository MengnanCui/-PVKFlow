"""领域层抛给接口层的异常。

**NotFound** = 用户要的东西不存在（样品、任务、处理、模块……）。接口层（`app/api/common.guard`）把它翻成 404。
它是 KeyError 的子类，老代码里的 `except KeyError` 照样接得住；但**普通的 KeyError 不再被当成 404** ——
那多半是程序自己的 bug（拿错了 dict 的键），原来会显示成「没有这个：xxx」，把 bug 藏住了（C073）。
"""
from __future__ import annotations


class NotFound(KeyError):
    def __str__(self) -> str:          # KeyError 的 str() 会给消息加引号；这里要原样的人话
        return str(self.args[0]) if self.args else "没有找到"
