"""样品集 —— 给一次选择起个名字存下来。

两种，都有用，语义完全不同：

  * **动态集**存筛选式。「B20 全批」—— 以后新导入的 B20 样品会自动进来。
  * **固定集**存 ID 快照。「论文图 3 那 12 个」—— 必须钉死，不能因为
    后来又导了几个样品就变。

搞混这两个会出事：论文里的图对应的样品集如果是动态的，重跑一次数字就变了。
所以建集合时必须显式选一种，没有默认的"聪明"行为。
"""
from __future__ import annotations

import json
from typing import Any

from app.storage import db, selection


class SetError(ValueError):
    pass


def create(name: str, kind: str, filter_: dict | None = None,
           sample_ids: list[str] | None = None, note: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise SetError("样品集要有名字")
    if kind not in ("dynamic", "pinned"):
        raise SetError("kind 只能是 dynamic（随新数据生长）或 pinned（钉死快照）")

    flt = selection.normalize(filter_ or {})
    if kind == "pinned":
        ids = list(sample_ids) if sample_ids else selection.sample_ids(flt)
        if not ids:
            raise SetError("固定集不能是空的")
    else:
        ids = []
        if not flt:
            raise SetError("动态集需要一个筛选式，否则它就是「全部样品」")

    if db.query_one("SELECT 1 FROM sample_set WHERE name = ?", (name,)):
        raise SetError(f"已经有叫「{name}」的样品集了")

    sid = db.new_id("set")
    now = db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO sample_set(set_id, name, kind, filter_json, pinned_ids_json,"
            " note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, name, kind, json.dumps(flt, ensure_ascii=False),
             json.dumps(ids), note or None, now, now))
    return get(sid)


def get(set_id: str) -> dict:
    row = db.query_one("SELECT * FROM sample_set WHERE set_id = ?", (set_id,))
    if not row:
        raise KeyError(f"没有这个样品集：{set_id}")
    return _hydrate(row)


def _hydrate(row: dict) -> dict:
    flt = json.loads(row["filter_json"] or "{}")
    ids = json.loads(row["pinned_ids_json"] or "[]")
    row["filter"] = flt
    row["pinned_ids"] = ids
    # 动态集的数量是现算的 —— 它本来就会随数据变
    row["count"] = len(ids) if row["kind"] == "pinned" else selection.count(flt)
    return row


def list_all() -> list[dict]:
    return [_hydrate(r) for r in db.query(
        "SELECT * FROM sample_set ORDER BY updated_at DESC")]


def resolve(set_id: str) -> dict:
    """样品集 → 可执行的筛选式。

    固定集展开成 ids，动态集直接用它的筛选式。
    """
    s = get(set_id)
    if s["kind"] == "pinned":
        return {"ids": s["pinned_ids"]}
    return s["filter"]


def delete(set_id: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM sample_set WHERE set_id = ?", (set_id,))


def rename(set_id: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise SetError("样品集要有名字")
    with db.tx() as c:
        c.execute("UPDATE sample_set SET name=?, updated_at=? WHERE set_id=?",
                  (name, db.now(), set_id))
    return get(set_id)


def freeze(set_id: str) -> dict:
    """把动态集在此刻钉死成固定集。要发论文了就用这个。"""
    s = get(set_id)
    if s["kind"] == "pinned":
        return s
    ids = selection.sample_ids(s["filter"])
    with db.tx() as c:
        c.execute("UPDATE sample_set SET kind='pinned', pinned_ids_json=?, updated_at=?"
                  " WHERE set_id=?", (json.dumps(ids), db.now(), set_id))
    return get(set_id)
