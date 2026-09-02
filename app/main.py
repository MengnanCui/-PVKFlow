"""应用入口。

`python -m app.main` 起服务并自动打开浏览器。run.bat / run.sh 就是调它。
"""
from __future__ import annotations

import logging
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import (artifacts, assist, batch, chat, files, results, selection,
                     settings, skills, spectra)
from app.api import modules as modules_api
from app.api.common import error_response
from app.modules.registry import reload as reload_modules, seed_template
from app.skills.registry import registry

log = logging.getLogger("hte")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.storage import db

    config.ensure_dirs()
    db.init()
    db.seed_defaults()
    registry.load_all()
    log.info("已加载 %d 个 skill", len(registry.all()))
    seed_template()          # 工作区里得真有个模板目录可以复制
    mods = reload_modules()
    log.info("已加载 %d 个功能模块", mods["count"])

    # 上次没跑完的任务不能永远显示 running —— 用户会一直等一个早就死了的东西
    from app import tasks as task_queue
    reaped = task_queue.reap_interrupted()
    if reaped:
        log.warning("有 %d 个任务因为上次重启被中断，已标记", reaped)
    for err in registry.errors:
        log.warning("skill 加载失败 %s：%s", err["source"], err["error"])
    # 模块加载失败不能静默 —— 同事装了个坏模块，得看得见是哪一个、为什么
    for err in mods["errors"]:
        log.warning("模块加载失败 %s：%s", err["source"], err["error"])
    yield
    from app import tasks as task_queue
    task_queue.shutdown()
    db.close()


app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

for router in (files.router, skills.router, results.router, artifacts.router,
               assist.router, settings.router, spectra.router,
               selection.router, batch.router, batch.tasks_router,
               modules_api.router,
               chat.router):
    app.include_router(router)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return error_response(exc)


# pydantic 的边界报错原文是英文（"Input should be greater than or equal to 1"）。
_BOUND = {"greater_than_equal": ("不小于", "ge"), "less_than_equal": ("不大于", "le"),
          "greater_than": ("大于", "gt"), "less_than": ("小于", "lt")}


@app.exception_handler(RequestValidationError)
async def bad_request(_: Request, exc: RequestValidationError):
    """参数不合法。**翻成这个平台自己那套错误结构。**

    FastAPI 默认吐的是 pydantic 的原始结构（`{"detail":[{"type":"string_pattern_
    mismatch","loc":[...],"msg":"String should match pattern '^(integral|slope)$'"}]}`）——
    前端认的是 `{"error":{message,kind,detail}}`，认不出来就只能显示
    「请求失败（422）」外加一坨英文 JSON。

    整个平台只有这一处说英语，而它偏偏是「你哪里填错了」这种最需要说人话的地方。
    """
    parts = []
    for e in exc.errors():
        where = ".".join(str(x) for x in (e.get("loc") or [])[1:]) or "参数"
        ctx, kind, got = e.get("ctx") or {}, e.get("type", ""), e.get("input")
        if ctx.get("pattern"):
            # `^(integral|slope|ot)$` → `integral / slope / ot`，
            # 正则本身对用户没有意义，能选哪几个才有
            opts = ctx["pattern"].strip("^$()").replace("|", " / ")
            parts.append(f"{where} 只能是 {opts}，给的是 {got!r}")
        elif kind == "missing":
            parts.append(f"少了一个参数：{where}")
        elif kind.endswith("_parsing"):
            parts.append(f"{where} 要是数字，给的是 {got!r}")
        elif kind in _BOUND:
            sign, key = _BOUND[kind]
            parts.append(f"{where} 要{sign} {ctx.get(key)}，给的是 {got!r}")
        else:
            parts.append(f"{where}：{e.get('msg') or '不合法'}")
    return JSONResponse(
        status_code=400,
        content={"error": {"message": "；".join(parts) or "参数不合法",
                           "kind": "bad_request", "detail": ""}},
    )


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("未处理的异常")
    return JSONResponse(
        status_code=500,
        content={"error": {"message": f"服务端出错：{exc}", "kind": "internal", "detail": ""}},
    )


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "workspace": str(config.WORKSPACE),
        "skills": len(registry.all()),
        "skill_errors": len(registry.errors),
    }


# 静态资源。SPA 路由交给前端自己处理，所有未命中路径回落到 index.html
app.mount("/assets", StaticFiles(directory=config.WEB_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/{path:path}")
def spa(path: str):
    # 未命中的 /api/* 要返回 JSON 404，不能回落到 index.html——
    # 否则前端会拿到一坨 HTML 再报解析错误，把真正的问题盖住
    if path.startswith("api/"):
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"没有这个接口：/{path}",
                               "kind": "not_found", "detail": ""}},
        )
    target = config.WEB_DIR / path
    if target.is_file() and config.WEB_DIR in target.resolve().parents:
        return FileResponse(target)
    return FileResponse(config.WEB_DIR / "index.html")


def _pick_port(host: str, port: int, attempts: int = 20) -> int:
    """端口被占了就往后找一个，别让用户对着报错发愣。"""
    for candidate in range(port, port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return port


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    config.ensure_dirs()

    port = _pick_port(config.HOST, config.PORT)
    url = f"http://{config.HOST}:{port}"

    print()
    print(f"  {config.APP_NAME} 已启动")
    print(f"  {url}")
    print(f"  工作区：{config.WORKSPACE}")
    print("  按 Ctrl+C 停止")
    print()

    if not __import__("os").environ.get("HTE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=config.HOST, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
