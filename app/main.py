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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import artifacts, assist, files, results, settings, skills, spectra
from app.api.common import error_response
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
    for err in registry.errors:
        log.warning("skill 加载失败 %s：%s", err["source"], err["error"])
    yield
    db.close()


app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

for router in (files.router, skills.router, results.router, artifacts.router,
               assist.router, settings.router, spectra.router):
    app.include_router(router)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return error_response(exc)


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
