"""文件导入与浏览。

为什么有一个服务端的目录浏览接口：浏览器出于安全不会告诉网页文件的真实路径，
而「图像不复制、原地引用」必须知道真实路径。所以主通道是
「在应用里浏览本机目录 → 选中 → 导入」，拖拽上传作为便捷补充（会全部复制）。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, File, Query, UploadFile

from app import config
from app.api import bodies as B
from app.api.common import ApiError, guard
from app.storage import artifacts, catalog, db, ingest, naming

router = APIRouter(prefix="/api/files", tags=["files"])

# 拖拽上传一次最多多大（全部文件加起来）。大批量走「浏览本机目录」导入
UPLOAD_MAX_BYTES = 512 * 2**20


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
def scan(body: B.ScanBody) -> dict:
    """扫描 + 分类 + 样品匹配预览。不写任何东西，给人确认用。

    mode="folders"：主文件夹 → 每个含 Data.csv 的子文件夹算一次测量。
    mode="files"（默认）：按文件名走命名规则，老流程不变。
    """
    payload = B.dump(body)
    path = payload.get("path")
    if not path:
        raise ApiError("缺少 path", 400)
    mode = (payload.get("mode") or "files").strip()
    if mode == "folders":
        return guard(ingest.scan_folders, path)
    if mode != "files":
        raise ApiError(f"不认识的导入模式：{mode}。可用的是 files / folders", 400)
    recursive = bool(payload.get("recursive", True))
    return guard(ingest.scan_preview, path, recursive)


@router.post("/import")
def import_files(body: B.ImportBody) -> dict:
    """把（可能被用户改过的）预览行写进库。"""
    payload = B.dump(body)
    entries = payload.get("files") or []
    if not entries:
        raise ApiError("没有要导入的文件", 400)
    report = guard(ingest.ingest_paths, entries, payload.get("source_hint", ""))
    return report.as_dict()


@router.post("/upload")
def upload(files: list[UploadFile] = File(...)) -> dict:
    """拖拽上传通道。

    浏览器不提供原始路径，所以**这里一律复制**，图像也一样。
    需要图像原地引用请用「浏览本机目录」导入。

    写成同步函数：FastAPI 会放到线程池里跑。解析、写库、生成缩略图都是阻塞的活，
    原来写成 async 直接在事件循环里做，上传大文件期间整个界面（包括进度条）都卡住（自查 §6 #26）。
    """
    payloads, total = [], 0
    for f in files:
        # 一次上传的总大小有上限：拖拽这条路要把文件读进内存，拖进一整个数据盘会把这台电脑的内存吃光。
        # 大批量请走「浏览本机目录」导入（只读文件头、按需复制，C074）
        data = f.file.read(UPLOAD_MAX_BYTES - total + 1)
        total += len(data)
        if total > UPLOAD_MAX_BYTES:
            raise ApiError(f"一次拖进来的文件超过 {UPLOAD_MAX_BYTES // 2**20} MB。"
                           "大批量请用「导入 → 浏览本机目录」，不走浏览器。", 413, "too_big")
        payloads.append((f.filename or "unnamed", data))
    report = ingest.ingest_uploads(payloads, source_hint="浏览器上传")
    return report.as_dict()


@router.get("")
def list_files(
    q: str = "", sample_id: str | None = None, batch_id: str | None = None,
    ext: str | None = None, status: str | None = None,
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
) -> dict:
    return artifacts.search(q=q, sample_id=sample_id, batch_id=batch_id, ext=ext,
                            status=status, limit=limit, offset=offset)


@router.get("/facets")
def facets() -> dict:
    return catalog.file_facets()


@router.get("/{artifact_id}")
def detail(artifact_id: str) -> dict:
    row = artifacts.get(artifact_id)
    if not row:
        raise ApiError(f"文件不存在：{artifact_id}", 404, "not_found")
    return row


@router.post("/verify")
def verify() -> dict:
    """巡检原始文件（引用型 + 复制型）还在不在；找不到的列出来（最多 200 条，总数照实给）。"""
    return ingest.verify_references()


@router.post("/naming/preview")
def naming_preview(body: B.NamingPreviewBody) -> dict:
    """试一条命名规则，看它能不能正确解析出样品名。"""
    payload = B.dump(body)
    paths = payload.get("paths") or []
    rules = payload.get("rules") or db.get_setting("naming_rules", list(config.DEFAULTS.naming_rules))
    return {"rules": rules, "rows": naming.preview(paths, rules)}
