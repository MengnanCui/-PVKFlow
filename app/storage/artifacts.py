"""artifact 的查询与本地路径解析。

调用方（skill、API）只认 artifact_id，不该关心文件到底是被复制了还是原地引用。
`local_path()` 负责把这层差异吃掉。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app import config
from app.storage import db


def get(artifact_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,))
    if row:
        try:
            row["meta"] = json.loads(row.get("meta_json") or "{}")
        except json.JSONDecodeError:
            row["meta"] = {}
    return row


def local_path(artifact_id: str) -> Path:
    """拿到可以直接打开的绝对路径。文件不在了就明确报错。"""
    row = get(artifact_id)
    if not row:
        raise KeyError(f"文件不存在于索引：{artifact_id}")

    if row["storage_mode"] == "copied" and row["stored_path"]:
        p = config.WORKSPACE / row["stored_path"]
    else:
        p = Path(row["original_path"] or "")

    if not p.is_file():
        with db.tx() as c:
            c.execute("UPDATE artifact SET status='missing' WHERE artifact_id=?", (artifact_id,))
        raise FileNotFoundError(
            f"文件已不在原位：{row['filename']}\n"
            f"记录路径：{p}\n"
            f"（这是一个 {'引用' if row['storage_mode'] == 'referenced' else '复制'} 型文件）"
        )
    return p


def register_derived(
    analysis_run_id: str,
    name: str,
    data: bytes,
    mime: str = "image/svg+xml",
    sample_id: str | None = None,
) -> dict:
    """登记 skill 产出的派生文件（图等）。"""
    import hashlib

    config.ensure_dirs()
    sha = hashlib.sha256(data).hexdigest()
    ext = {"image/svg+xml": ".svg", "image/png": ".png", "image/jpeg": ".jpg",
           "text/plain": ".txt", "application/json": ".json"}.get(mime, ".bin")
    rel = Path("derived") / "figures" / f"{sha}{ext}"
    target = config.WORKSPACE / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)

    aid = db.new_id("art")
    with db.tx() as c:
        c.execute(
            "INSERT INTO artifact(artifact_id, kind, storage_mode, sha256, original_path,"
            " display_path, stored_path, filename, ext, mime, size, status, meta_json,"
            " sample_id, produced_by, created_at)"
            " VALUES(?,'derived','copied',?,NULL,?,?,?,?,?,?, 'ok','{}',?,?,?)",
            (aid, sha, name, rel.as_posix(), name, ext, mime, len(data),
             sample_id, analysis_run_id, db.now()),
        )
    return {"artifact_id": aid, "name": name, "mime": mime, "path": rel.as_posix()}


def search(
    q: str = "",
    kind: str = "raw",
    sample_id: str | None = None,
    batch_id: str | None = None,
    ext: str | None = None,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    where, params = ["a.kind = ?"], [kind]
    if q:
        where.append("(a.filename LIKE ? OR a.display_path LIKE ? OR a.original_path LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if sample_id:
        where.append("a.sample_id = ?")
        params.append(sample_id)
    if batch_id:
        where.append("a.batch_id = ?")
        params.append(batch_id)
    if ext:
        where.append("a.ext = ?")
        params.append(ext.lower())
    if status:
        where.append("a.status = ?")
        params.append(status)
    clause = "WHERE " + " AND ".join(where)

    total = db.scalar(f"SELECT COUNT(*) FROM artifact a {clause}", tuple(params)) or 0
    rows = db.query(
        f"SELECT a.artifact_id, a.filename, a.display_path, a.ext, a.size, a.storage_mode,"
        f"       a.status, a.thumb_path, a.mime, a.created_at, a.sample_id,"
        f"       s.name AS sample_name, s.batch AS sample_batch"
        f" FROM artifact a LEFT JOIN sample s ON s.sample_id = a.sample_id"
        f" {clause} ORDER BY a.created_at DESC, a.rowid DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


def extension_facets() -> list[dict]:
    return db.query(
        "SELECT ext, COUNT(*) AS n FROM artifact WHERE kind='raw'"
        " GROUP BY ext ORDER BY n DESC LIMIT 30"
    )
