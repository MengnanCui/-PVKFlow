"""批处理与后台任务。"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app import batch as batch_mod
from app import batch_history as history
from app import batch_views as views
from app import tasks
from app.api import bodies as B
from app.api.common import ApiError, guard
from app.errors import NotFound
from app.storage import selection

router = APIRouter(prefix="/api/batch", tags=["batch"])


@router.post("/run")
def run(body: B.BatchRunBody) -> dict:
    """提交一次批处理。

    配方就是单样品页当前那套参数 —— 不做第二套参数表单。
    立刻返回 task_id，前端轮询进度。
    """
    payload = B.dump(body)
    try:
        return batch_mod.submit_batch(payload.get("filter"), set_id=payload.get("set_id") or None,
                                      recipe=payload.get("recipe"), title=payload.get("title") or "")
    except NotFound as exc:
        raise ApiError(str(exc), 404, "not_found") from exc
    except selection.FilterError as exc:
        raise ApiError(str(exc), 400, "bad_filter") from exc
    except batch_mod.EmptySelection as exc:
        raise ApiError(str(exc), 400, "empty_selection") from exc
    except ValueError as exc:
        raise ApiError(str(exc), 400, "bad_recipe") from exc


@router.post("/preview")
def preview(body: B.FilterBody | None = None) -> dict:
    """跑之前先看会处理多少个、有多少个没有矩阵。"""
    payload = B.dump(body)
    try:
        flt = selection.normalize(payload.get("filter") or {})
    except selection.FilterError as exc:
        raise ApiError(str(exc), 400, "bad_filter") from exc
    total = selection.count(flt)
    with_matrix = selection.count({**flt, "has_matrix": True})
    # 默认配方也从这里给：它由已装模块的控件声明决定（app/batch.py Recipe），
    # 前端不再自己抄一份 —— 抄的那份就是 780–1050 / 775–1120 两套并存的来源。
    return {"total": total, "with_matrix": with_matrix,
            "without_matrix": total - with_matrix,
            "recipe_defaults": batch_mod.Recipe().as_dict()}


@router.get("/runs")
def runs(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"runs": batch_mod.recent_batches(limit)}


@router.get("/runs/{parent_run_id}")
def detail(parent_run_id: str) -> dict:
    return guard(views.detail, parent_run_id)


@router.patch("/runs/{parent_run_id}")
def rename_run(parent_run_id: str, body: B.RenameRunBody) -> dict:
    """给一次批处理改名（对比历史里显示的名字）。"""
    return guard(history.rename, parent_run_id, body.title)


@router.get("/runs/{parent_run_id}/footprint")
def run_footprint(parent_run_id: str) -> dict:
    """删掉这次会带走什么：几个样品、几条关键结果（其中几条是当前值、几条审核过）、几张长表、几条钉住的分析。"""
    return guard(history.footprint, parent_run_id)


@router.delete("/runs/{parent_run_id}")
def delete_run(parent_run_id: str) -> dict:
    """删掉一次批处理：运行、它写的关键结果和长表、钉住的分析（原始数据和样品不动；还在跑的不能删）。"""
    return guard(history.delete, parent_run_id)


@router.get("/runs/{parent_run_id}/axes")
def axes(parent_run_id: str) -> dict:
    """这批数据的波长轴和时间轴范围（读不到返回 available=false，不报错）。"""
    return guard(views.axes, parent_run_id)


@router.get("/runs/{parent_run_id}/curves")
def curves(parent_run_id: str,
           column: str = Query("integral", pattern=views.COLUMN_RE),
           max_series: int = Query(400, ge=1, le=5000)) -> dict:
    """批处理的曲线，按样品分组返回（最多 max_series 条，并说明截断了多少）。"""
    return guard(views.curve_series, parent_run_id, column, max_series)


@router.get("/runs/{parent_run_id}/slices")
def slices(parent_run_id: str,
           windows: str = Query("0:1", description="逗号分隔的 起:止，单位秒")) -> dict:
    """不同样品在若干个时间窗内的平均膜厚（口径见 app/batch_views.slices）。"""
    return guard(views.slices, parent_run_id, windows)


@router.get("/runs/{parent_run_id}/export")
def export_script(parent_run_id: str,
                  column: str = Query("integral", pattern=views.COLUMN_RE),
                  mode: str = Query("overlay", pattern="^(overlay|band)$"),
                  group_by: str = Query("batch", pattern="^(batch|none)$"),
                  max_series: int = Query(1200, ge=1, le=5000)) -> Response:
    """打包 plot.py + data.csv + README.md 下载 —— 平台内置图型不够用时，拿脚本走人随便改。"""
    blob, name = guard(views.export_bundle, parent_run_id, column, mode, group_by, max_series)
    return Response(content=blob, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/runs/{parent_run_id}/export/preview")
def export_preview(parent_run_id: str,
                   column: str = Query("integral", pattern=views.COLUMN_RE),
                   mode: str = Query("overlay", pattern="^(overlay|band)$"),
                   group_by: str = Query("batch", pattern="^(batch|none)$")) -> dict:
    """先读脚本，再决定要不要下载。"""
    return guard(views.export_preview, parent_run_id, column, mode, group_by)


# ------------------------------------------------------------------ 任务
tasks_router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@tasks_router.get("")
def list_tasks(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"tasks": tasks.recent(limit)}


@tasks_router.get("/{task_id}")
def get_task(task_id: str) -> dict:
    return guard(tasks.get, task_id)


@tasks_router.post("/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    """取消是「停在这儿」，不是「当没发生过」—— 已跑完的样品结果保留。"""
    return guard(tasks.cancel, task_id)
