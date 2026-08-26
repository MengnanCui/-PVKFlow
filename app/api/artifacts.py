"""原始 / 派生文件的内容读取。"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, PlainTextResponse

from app import config
from app.api.common import ApiError, guard
from app.parsers import sniff
from app.storage import artifacts

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}/raw")
def raw(artifact_id: str):
    path = guard(artifacts.local_path, artifact_id)
    row = artifacts.get(artifact_id) or {}
    return FileResponse(path, media_type=row.get("mime") or "application/octet-stream",
                        filename=row.get("filename"))


@router.get("/{artifact_id}/thumb")
def thumb(artifact_id: str):
    row = artifacts.get(artifact_id)
    if not row:
        raise ApiError(f"文件不存在：{artifact_id}", 404, "not_found")
    if not row.get("thumb_path"):
        raise ApiError("这个文件没有缩略图", 404, "no_thumb")
    p = config.WORKSPACE / row["thumb_path"]
    if not p.is_file():
        raise ApiError("缩略图已丢失", 404, "no_thumb")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/{artifact_id}/head")
def head(artifact_id: str, lines: int = Query(60, le=500)) -> PlainTextResponse:
    """看文件前几行 —— 判断分隔符/抬头对不对时最直接的办法。"""
    path = guard(artifacts.local_path, artifact_id)
    try:
        text, _ = sniff.read_text(path, max_bytes=200_000)
    except Exception as exc:
        raise ApiError(f"无法作为文本读取：{exc}", 400, "not_text") from exc
    return PlainTextResponse("\n".join(text.splitlines()[:lines]))


@router.get("/{artifact_id}/preview")
def preview(artifact_id: str, rows: int = Query(200, le=5000)) -> dict:
    """把文件解析成表格预览。前端表格与图表都吃这个。"""
    path = guard(artifacts.local_path, artifact_id)
    row = artifacts.get(artifact_id) or {}
    ext = (row.get("ext") or "").lower()

    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}:
        return {"kind": "image", "artifact_id": artifact_id,
                "meta": row.get("meta", {}), "has_thumb": bool(row.get("thumb_path"))}

    try:
        df, s = sniff.load_frame(path, max_rows=rows)
    except Exception as exc:
        try:
            text, _ = sniff.read_text(path, max_bytes=40_000)
        except Exception as exc2:
            raise ApiError(f"无法预览：{exc2}", 400, "no_preview") from exc2
        return {"kind": "text", "text": "\n".join(text.splitlines()[:200]),
                "note": f"未能解析为表格：{exc}"}

    import pandas as pd

    clean = df.where(pd.notna(df), None)
    return {
        "kind": "table",
        "columns": [str(c) for c in df.columns],
        "dtypes": {str(c): ("numeric" if pd.api.types.is_numeric_dtype(df[c]) else "text")
                   for c in df.columns},
        "rows": clean.values.tolist(),
        "n_rows": int(len(df)),
        "sniffed": s.as_dict() if hasattr(s, "as_dict") else {},
    }
