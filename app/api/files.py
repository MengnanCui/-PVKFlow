"""文件导入与浏览。

为什么有一个服务端的目录浏览接口：浏览器出于安全不会告诉网页文件的真实路径，
而「图像不复制、原地引用」必须知道真实路径。所以主通道是
「在应用里浏览本机目录 → 选中 → 导入」，拖拽上传作为便捷补充（会全部复制）。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Body, File, Query, UploadFile

from app import config
from app.api.common import ApiError, guard
from app.storage import artifacts, db, ingest, naming

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/roots")
def roots() -> dict:
    """给目录浏览器一个起点：盘符（Windows）或常用目录。"""
    out = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            p = Path(f"{letter}:\\")
            if p.exists():
                out.append({"name": f"{letter}:", "path": str(p)})
    else:
        out.append({"name": "/", "path": "/"})
    home = Path.home()
    for name, p in (("主目录", home), ("桌面", home / "Desktop"), ("下载", home / "Downloads"),
                    ("文档", home / "Documents")):
        if p.exists():
            out.append({"name": name, "path": str(p)})
    return {"roots": out, "cwd": str(Path.cwd())}


@router.get("/browse")
def browse(path: str = Query(...), show_hidden: bool = False) -> dict:
    """列一个目录。只读，不改任何东西。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise ApiError(f"路径不存在：{p}", 404, "not_found")
    if p.is_file():
        p = p.parent

    dirs, files = [], []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                else:
                    dirs_stat = entry.stat()
                    files.append({
                        "name": entry.name, "path": str(entry),
                        "size": dirs_stat.st_size, "ext": entry.suffix.lower(),
                    })
            except OSError:
                continue
    except PermissionError as exc:
        raise ApiError(f"没有权限读取：{p}", 403, "forbidden") from exc

    return {
        "path": str(p.resolve()),
        "parent": str(p.parent) if p.parent != p else None,
        "dirs": dirs,
        "files": files,
    }


@router.post("/scan")
def scan(payload: dict = Body(...)) -> dict:
    """扫描 + 分类 + 样品匹配预览。不写任何东西，给人确认用。"""
    path = payload.get("path")
    if not path:
        raise ApiError("缺少 path", 400)
    recursive = bool(payload.get("recursive", True))
    return guard(ingest.scan_preview, path, recursive)


@router.post("/import")
def import_files(payload: dict = Body(...)) -> dict:
    """把（可能被用户改过的）预览行写进库。"""
    entries = payload.get("files") or []
    if not entries:
        raise ApiError("没有要导入的文件", 400)
    report = guard(ingest.ingest_paths, entries, payload.get("source_hint", ""))
    return report.as_dict()


@router.post("/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    """拖拽上传通道。

    浏览器不提供原始路径，所以**这里一律复制**，图像也一样。
    需要图像原地引用请用「浏览本机目录」导入。
    """
    payloads = []
    for f in files:
        data = await f.read()
        payloads.append((f.filename or "unnamed", data))
    report = ingest.ingest_uploads(payloads, source_hint="浏览器上传")
    return report.as_dict()


@router.get("")
def list_files(
    q: str = "", sample_id: str | None = None, batch_id: str | None = None,
    ext: str | None = None, status: str | None = None,
    limit: int = Query(200, le=1000), offset: int = 0,
) -> dict:
    return artifacts.search(q=q, sample_id=sample_id, batch_id=batch_id, ext=ext,
                            status=status, limit=limit, offset=offset)


@router.get("/facets")
def facets() -> dict:
    return {
        "extensions": artifacts.extension_facets(),
        "batches": db.query(
            "SELECT batch_id, source_hint, file_count, created_at FROM import_batch"
            " ORDER BY created_at DESC LIMIT 30"
        ),
        "samples": db.query(
            "SELECT s.sample_id, s.name, s.batch,"
            " (SELECT COUNT(*) FROM artifact a WHERE a.sample_id = s.sample_id) AS n_files"
            " FROM sample s ORDER BY s.name LIMIT 500"
        ),
    }


@router.get("/{artifact_id}")
def detail(artifact_id: str) -> dict:
    row = artifacts.get(artifact_id)
    if not row:
        raise ApiError(f"文件不存在：{artifact_id}", 404, "not_found")
    return row


@router.post("/verify")
def verify() -> dict:
    """巡检引用型文件是否还在原位。"""
    return ingest.verify_references()


@router.post("/naming/preview")
def naming_preview(payload: dict = Body(...)) -> dict:
    """试一条命名规则，看它能不能正确解析出样品名。"""
    paths = payload.get("paths") or []
    rules = payload.get("rules") or db.get_setting("naming_rules", list(config.DEFAULTS.naming_rules))
    return {"rules": rules, "rows": naming.preview(paths, rules)}
