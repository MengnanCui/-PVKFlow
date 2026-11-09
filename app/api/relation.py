"""构效关系：任选两个字段看相关，按批次看分布。数据在 app/storage/relation.py。"""
from __future__ import annotations

from fastapi import APIRouter, Body

from app.api.common import ApiError, guard
from app.storage import relation

router = APIRouter(prefix="/api/relation", tags=["relation"])


def _need(payload: dict, key: str) -> str:
    v = str(payload.get(key) or "").strip()
    if not v:
        raise ApiError(f"要指定 {key}", 400)
    return v


@router.post("/pairs")
def pairs(payload: dict = Body(...)) -> dict:
    """{x, y, filter?} → 每个样品一个点 + 皮尔逊 / 斯皮尔曼（带 p 值）。"""
    return guard(relation.pairs, _need(payload, "x"), _need(payload, "y"), payload.get("filter"))


@router.post("/groups")
def groups(payload: dict = Body(...)) -> dict:
    """{y, by?, filter?} → 每组的箱线图统计，离群点带样品。"""
    return guard(relation.groups, _need(payload, "y"), payload.get("by") or "batch",
                 payload.get("filter"))
