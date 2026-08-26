"""API 层共用的东西：统一错误结构、分页参数。

错误响应永远是这个形状，前端只需要写一遍处理逻辑：

    { "error": { "message": "人话说明", "kind": "not_found", "detail": "…" } }
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class ApiError(HTTPException):
    def __init__(self, message: str, status: int = 400, kind: str = "bad_request",
                 detail: str = "") -> None:
        super().__init__(status_code=status,
                         detail={"message": message, "kind": kind, "detail": detail})


def error_response(exc: HTTPException) -> JSONResponse:
    d = exc.detail
    if isinstance(d, dict) and "message" in d:
        payload = d
    else:
        payload = {"message": str(d), "kind": "error", "detail": ""}
    return JSONResponse(status_code=exc.status_code, content={"error": payload})


def guard(fn, *args, **kwargs) -> Any:
    """把领域层的异常翻译成带人话的 API 错误。"""
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise ApiError(str(exc).strip("'\""), 404, "not_found") from exc
    except FileNotFoundError as exc:
        raise ApiError(str(exc), 404, "file_missing") from exc
    except (ValueError, TypeError) as exc:
        raise ApiError(str(exc), 400, "invalid") from exc
    except NotImplementedError as exc:
        raise ApiError(str(exc), 501, "not_ready") from exc
    except RuntimeError as exc:
        raise ApiError(str(exc), 500, "failed") from exc
