"""批处理与后台任务。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query
from fastapi.responses import Response

from app import batch as batch_mod
from app import tasks
from app.api.common import ApiError, guard
from app.plotting import script_export
from app.storage import selection, sets

router = APIRouter(prefix="/api/batch", tags=["batch"])


@router.post("/run")
def run(payload: dict = Body(...)) -> dict:
    """提交一次批处理。

    配方就是单样品页当前那套参数 —— 不做第二套参数表单。
    立刻返回 task_id，前端轮询进度。
    """
    raw_filter = payload.get("filter")
    if payload.get("set_id"):
        raw_filter = guard(sets.resolve, payload["set_id"])

    try:
        flt = selection.normalize(raw_filter or {})
    except selection.FilterError as exc:
        raise ApiError(str(exc), 400, "bad_filter") from exc

    n = selection.count({**flt, "has_matrix": True})
    if not n:
        raise ApiError("这个筛选式没有命中任何带光谱矩阵的样品", 400, "empty_selection")

    try:
        recipe = batch_mod.Recipe.from_dict(payload.get("recipe"))
    except ValueError as exc:
        raise ApiError(str(exc), 400, "bad_recipe") from exc

    task = tasks.submit(
        batch_mod.BATCH_SKILL_ID,
        {"filter": flt, "recipe": recipe.as_dict()},
        title=payload.get("title") or f"批处理 {n} 个样品",
    )
    return {"task": task, "n_samples": n, "recipe": recipe.as_dict()}


@router.post("/preview")
def preview(payload: dict = Body(default={})) -> dict:
    """跑之前先看会处理多少个、有多少个没有矩阵。"""
    try:
        flt = selection.normalize(payload.get("filter") or {})
    except selection.FilterError as exc:
        raise ApiError(str(exc), 400, "bad_filter") from exc
    total = selection.count(flt)
    with_matrix = selection.count({**flt, "has_matrix": True})
    return {"total": total, "with_matrix": with_matrix,
            "without_matrix": total - with_matrix}


@router.get("/runs")
def runs(limit: int = Query(20, le=100)) -> dict:
    return {"runs": batch_mod.recent_batches(limit)}


@router.get("/runs/{parent_run_id}")
def detail(parent_run_id: str) -> dict:
    return guard(batch_mod.batch_detail, parent_run_id)


def _load_curves(parent_run_id: str, column: str):
    """读出这次批处理的长表。curves 和 export 走的是同一份数据。"""
    detail_ = guard(batch_mod.batch_detail, parent_run_id)
    tables = [t for t in detail_["tables"] if t["name"] == "batch_curves"]
    if not tables:
        raise ApiError("这次批处理没有留下曲线表", 404, "no_curves")

    import pandas as pd

    from app import config

    path = config.WORKSPACE / tables[0]["path"]
    if not path.is_file():
        raise ApiError(f"曲线表文件缺失：{tables[0]['path']}", 404, "missing_table")
    df = pd.read_csv(path) if path.suffix == ".csv" else pd.read_parquet(path)
    if column not in df.columns:
        raise ApiError(f"这张表里没有 {column} 列", 400, "bad_column")
    return detail_, tables[0], df


@router.get("/runs/{parent_run_id}/curves")
def curves(parent_run_id: str,
           column: str = Query("integral", pattern="^(integral|slope)$"),
           max_series: int = Query(400, ge=1, le=5000)) -> dict:
    """批处理的曲线，按样品分组返回。

    上千条曲线叠一张图是噪声不是图 —— 超过阈值时前端会降级成分位数带，
    所以这里控制一下返回的序列上限，并如实告知截断了多少。
    """
    _detail, table, df = _load_curves(parent_run_id, column)

    groups = list(df.groupby("sample_id", sort=False))
    truncated = max(0, len(groups) - max_series)
    series = []
    for sid, g in groups[:max_series]:
        y = g[column].tolist()
        series.append({
            "sample_id": sid,
            # 图例里只写 S1 的话，24 个批次的 S1 在图上分不出是哪一个
            "label": (f"{g['batch'].iloc[0]}/{g['sample_name'].iloc[0]}"
                      if g["batch"].iloc[0] else str(g["sample_name"].iloc[0])),
            "group": str(g["batch"].iloc[0] or ""),
            "x": [round(float(v), 4) for v in g["t"]],
            "y": [None if v is None or v != v else round(float(v), 6) for v in y],
        })
    return {"column": column, "series": series,
            "n_series": len(groups), "returned": len(series), "truncated": truncated,
            "table_id": table["table_id"]}


# 每个标签配一份英文：导出的脚本会被拷到别的机器上跑，那台机器不一定
# 装了中文字体。脚本里自己判断，装了用中文，没装退回英文。
_Y_LABELS = {
    "integral": ("积分强度 (a.u.·nm)", "Band integral (a.u.·nm)"),
    "slope": ("dI/dλ (a.u./nm)", "dI/dλ (a.u./nm)"),
}


@router.get("/runs/{parent_run_id}/export")
def export_script(parent_run_id: str,
                  column: str = Query("integral", pattern="^(integral|slope)$"),
                  mode: str = Query("overlay", pattern="^(overlay|band)$"),
                  group_by: str = Query("batch", pattern="^(batch|none)$"),
                  max_series: int = Query(1200, ge=1, le=5000)) -> Response:
    """打包 plot.py + data.csv + README.md 下载。

    这是灵活性的出口：平台内置图型不够用时，你拿脚本走人随便改。
    脚本里的样式逐字来自你的 matplotlib 规范 —— 论文里那张图是你自己
    能读、能改、能引用的代码画的。
    """
    detail_, _table, df = _load_curves(parent_run_id, column)

    # 一条曲线 = 一个 sample_id。样品的身份是 (名字, 批次)，S1 在每个批次
    # 里都有一个 —— 按名字截断或分组会把不同批次的同名样品合成一条。
    sids = list(dict.fromkeys(df["sample_id"]))
    if len(sids) > max_series:
        sids = sids[:max_series]
        df = df[df["sample_id"].isin(set(sids))]

    df = df.copy()
    df["label"] = [f"{b}/{n}" if b else str(n)
                   for b, n in zip(df["batch"].fillna(""), df["sample_name"])]
    cols = ["sample_id", "sample_name", "batch", "label", "t", column]
    csv_bytes = df[cols].to_csv(index=False).encode("utf-8-sig")

    zh_label, en_label = _Y_LABELS[column]
    blob = script_export.build_zip(
        csv_bytes=csv_bytes,
        column=column,
        y_label=zh_label,
        y_label_en=en_label,
        mode=mode,
        group_by=group_by,
        n_series=len(sids),
        n_rows=len(df),
        recipe=(detail_["run"].get("params") or {}).get("recipe") or {},
        run_id=parent_run_id,
        title=f"{zh_label} · {len(sids)} 个样品",
        title_en=f"{en_label} · {len(sids)} samples",
    )
    stem = f"hte-{column}-{parent_run_id[:8]}"
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}.zip"'},
    )


@router.get("/runs/{parent_run_id}/export/preview")
def export_preview(parent_run_id: str,
                   column: str = Query("integral", pattern="^(integral|slope)$"),
                   mode: str = Query("overlay", pattern="^(overlay|band)$"),
                   group_by: str = Query("batch", pattern="^(batch|none)$")) -> dict:
    """先读脚本，再决定要不要下载。

    这份脚本是「论文里那张图」的来源 —— 应该能在下载之前就读一眼，
    确认它画的是你想要的东西。
    """
    _detail, _table, df = _load_curves(parent_run_id, column)
    zh_label, en_label = _Y_LABELS[column]
    n_series = df["sample_id"].nunique()
    script = script_export.build_script(
        column=column, y_label=zh_label, y_label_en=en_label, mode=mode,
        group_by=group_by, n_series=int(n_series),
        title=f"{zh_label} · {n_series} 个样品",
        title_en=f"{en_label} · {n_series} samples")
    return {"script": script, "n_series": int(n_series), "n_rows": len(df),
            "columns": ["sample_id", "sample_name", "batch", "label", "t", column]}


# ------------------------------------------------------------------ 任务
tasks_router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@tasks_router.get("")
def list_tasks(limit: int = Query(20, le=100)) -> dict:
    return {"tasks": tasks.recent(limit)}


@tasks_router.get("/{task_id}")
def get_task(task_id: str) -> dict:
    return guard(tasks.get, task_id)


@tasks_router.post("/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    """取消是「停在这儿」，不是「当没发生过」—— 已跑完的样品结果保留。"""
    return guard(tasks.cancel, task_id)
