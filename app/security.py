"""只认这台机器上、这个平台自己的页面。

平台没有登录 —— 它的安全边界是「只有本机能连」。但只监听 127.0.0.1 不够：
你开着平台时打开的**任何网页**都在本机的浏览器里，都能朝 127.0.0.1 发请求。

两条路，两道闸：

1. **跨站请求**。浏览器允许网页向任意地址发「简单请求」（不带 Content-Type 的
   POST、multipart 表单）而不先问对方同不同意。响应网页读不到，但**副作用已经发生了**
   —— 改设置、导入文件、装模块（装模块会执行 module.py，等于执行任意代码）。
   闸：`/api/` 下的请求，带了 Origin 就必须和本页同源；浏览器标了
   `Sec-Fetch-Site: cross-site / same-site` 的一律拒。

2. **DNS 重绑定**。恶意域名先解析到它自己的服务器、再改解析到 127.0.0.1，
   于是浏览器认为那是「同源」，响应也读得到 —— 本机任意文件都能经
   `/api/files/browse`、`/api/artifacts/{id}/raw` 读出去。
   闸：Host 头必须是本机名字（127.0.0.1 / localhost / ::1）。恶意域名过不了这一条。

命令行、脚本、测试这些非浏览器的调用不带 Origin，也不带 Sec-Fetch-*，照常放行。
"""
from __future__ import annotations

import os

from fastapi.responses import JSONResponse

from app import config

LOCAL_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(host_header: str) -> str:
    """`127.0.0.1:8765` → `127.0.0.1`；`[::1]:8765` → `::1`。"""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h[1:]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def allowed_hostnames() -> set[str]:
    names = set(LOCAL_NAMES)
    if config.HOST not in ("0.0.0.0", "::", ""):
        names.add(config.HOST.lower())
    # 测试客户端用 testserver；有人确实要从别的名字访问时也走这里。逗号分隔。
    extra = os.environ.get("HTE_ALLOWED_HOSTS", "")
    names.update(x.strip().lower() for x in extra.split(",") if x.strip())
    return names


def check(method: str, path: str, headers) -> str | None:
    """不放行时返回原因（中文，给人看的），放行返回 None。"""
    host = headers.get("host", "")
    if _hostname(host) not in allowed_hostnames():
        return (f"拒绝了这个请求：它是冲着「{host}」来的，而平台只认本机地址"
                f"（127.0.0.1 / localhost）。如果你是从别的名字访问的，"
                f"把它加进环境变量 HTE_ALLOWED_HOSTS。")

    if not path.startswith("/api/"):
        return None      # 页面和静态文件：从别的网站点链接进来是正常的

    origin = headers.get("origin")
    if origin is not None:
        own = {f"http://{host}".lower(), f"https://{host}".lower()}
        if origin.lower() not in own:
            return (f"拒绝了来自「{origin}」的请求：只有平台自己的页面能调用接口。"
                    f"（你开着平台时打开的别的网页，也能朝本机发请求 —— 这一条就是防它的）")

    site = (headers.get("sec-fetch-site") or "").lower()
    if site in ("cross-site", "same-site"):
        return "拒绝了一个跨站请求：只有平台自己的页面能调用接口。"
    return None


async def middleware(request, call_next):
    reason = check(request.method, request.url.path, request.headers)
    if reason:
        return JSONResponse(status_code=403, content={
            "error": {"message": reason, "kind": "forbidden_origin", "detail": ""}})
    return await call_next(request)
