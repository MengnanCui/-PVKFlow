"""选择、分面、样品集。

接口上传的一律是**筛选式**，不是 sample_id 列表 —— 上千个样品时后者
放不进请求体，也没法复现。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api import bodies as B
from app.api.common import ApiError, guard
from app.storage import selection, sets

router = APIRouter(prefix="/api/selection", tags=["selection"])


def _filter(payload: dict) -> dict:
    try:
        return selection.normalize(payload.get("filter") or {})
    except selection.FilterError as exc:
        raise ApiError(str(exc), 400, "bad_filter") from exc


@router.post("/query")
def query(body: B.SelectionQueryBody | None = None) -> dict:
    """按筛选式取一页样品。服务端分页 —— 千级列表不能整个推给前端。"""
    payload = B.dump(body)
    flt = _filter(payload)
    return selection.page(
        flt,
        limit=min(int(payload.get("limit") or 100), 500),
        offset=max(int(payload.get("offset") or 0), 0),
        order=payload.get("order") or "name",
    )


@router.post("/count")
def count(body: B.FilterBody | None = None) -> dict:
    payload = B.dump(body)
    return {"count": selection.count(_filter(payload))}


@router.post("/facets")
def facets(body: B.FilterBody | None = None) -> dict:
    """分面与计数。

    每个分面的计数是在**去掉自己这一面之后**的筛选式下算的 ——
    否则选了 B20 之后其他批次全变成 0，就没法改选了。
    """
    payload = B.dump(body)
    return selection.facets(_filter(payload))


@router.post("/suggest")
def suggest(body: B.SuggestBody) -> dict:
    """手选几个之后，提议扩展成一条规则。返回的是筛选式，不是 ID 列表。"""
    payload = B.dump(body)
    ids = payload.get("sample_ids") or []
    if len(ids) < 2:
        return {"suggestions": []}
    return {"suggestions": selection.suggest_expansion(ids, _filter(payload))}


@router.post("/ids")
def ids(body: B.IdsBody | None = None) -> dict:
    """展开成 ID 列表。只在真的要执行时用。"""
    payload = B.dump(body)
    flt = _filter(payload)
    limit = min(int(payload.get("limit") or 5000), 50000)
    out = selection.sample_ids(flt, limit=limit)
    return {"sample_ids": out, "count": len(out),
            "truncated": len(out) >= limit}


# ------------------------------------------------------------------ 样品集
@router.get("/sets")
def list_sets() -> dict:
    return {"sets": sets.list_all()}


@router.post("/sets")
def create_set(body: B.CreateSetBody) -> dict:
    payload = B.dump(body)
    try:
        return sets.create(
            name=payload.get("name", ""),
            kind=payload.get("kind", "dynamic"),
            filter_=payload.get("filter"),
            sample_ids=payload.get("sample_ids"),
            note=payload.get("note", ""),
        )
    except sets.SetError as exc:
        raise ApiError(str(exc), 400, "bad_set") from exc
    except selection.FilterError as exc:
        raise ApiError(str(exc), 400, "bad_filter") from exc


@router.get("/sets/{set_id}")
def get_set(set_id: str) -> dict:
    return guard(sets.get, set_id)


@router.post("/sets/{set_id}/freeze")
def freeze_set(set_id: str) -> dict:
    """把动态集在此刻钉死。要发论文了就用这个。"""
    return guard(sets.freeze, set_id)


@router.post("/sets/{set_id}/rename")
def rename_set(set_id: str, body: B.RenameBody) -> dict:
    payload = B.dump(body)
    # SetError 是 ValueError → 400；不存在 → 404（guard 统一翻译）
    return guard(sets.rename, set_id, payload.get("name", ""))


@router.delete("/sets/{set_id}")
def delete_set(set_id: str) -> dict:
    """只删样品集本身（名字 + 筛选式 / 快照），样品和结果不动。"""
    return {"ok": True, **guard(sets.delete, set_id)}
