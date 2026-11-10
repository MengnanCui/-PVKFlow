"""artifact 的查询与本地路径解析。

调用方（skill、API）只认 artifact_id，不该关心文件到底是被复制了还是原地引用。
`local_path()` 负责把这层差异吃掉。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app import config
from app.errors import Invalid, NotFound
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
        raise NotFound(f"文件不存在于索引：{artifact_id}")

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


# ---------------------------------------------------------------- 光谱矩阵
def resolve_matrix_id(artifact_id: str) -> str:
    """允许直接用**样品 id**：`smp_xxx` → 这个样品最大的那份光谱矩阵。

    页面地址长这样：`#sample/art_xxx`。可从界面上能复制到的 id 常常是 `smp_xxx`
    （样品列表、失败样品表里显示的都是它）—— 收到样品 id 时翻成它的光谱矩阵，
    而不是回一句没说到点子上的「文件不存在：smp_xxx」。
    """
    if not artifact_id.startswith("smp_"):
        return artifact_id
    row = db.query_one(
        "SELECT artifact_id FROM artifact WHERE sample_id=? AND is_matrix=1"
        " ORDER BY size DESC LIMIT 1", (artifact_id,))
    if row:
        return row["artifact_id"]
    exists = db.query_one("SELECT 1 FROM sample WHERE sample_id=?", (artifact_id,))
    raise NotFound(f"这个样品没有光谱矩阵：{artifact_id}" if exists else f"没有这个样品：{artifact_id}")


def load_matrix(artifact_id: str):
    """读一份光谱矩阵（带缓存）。样品页、模块计算都走这里（原来是路由里的私有函数，被别的路由跨文件 import，C074）。

    只有复制进工作区的（只读副本，内容不会变）才信库里记的 sha；原地引用的文件可能被仪器软件改过，
    得现算 —— 不然样品页拿到旧缓存、批处理算的是新内容，两边对不上（C069）。
    """
    from app.parsers import matrix

    artifact_id = resolve_matrix_id(artifact_id)
    row = get(artifact_id)
    if not row:
        raise NotFound(f"文件不存在：{artifact_id}")
    path = local_path(artifact_id)
    known = row.get("sha256") if row.get("storage_mode") == "copied" else None
    try:
        return matrix.load_cached(path, known)
    except ValueError as exc:
        raise Invalid(str(exc), "not_a_matrix") from exc
    except Exception as exc:                    # noqa: BLE001 —— 解析器里什么都可能出，统一说「解析失败」
        raise Invalid(f"解析光谱矩阵失败：{exc}", "parse_failed") from exc
