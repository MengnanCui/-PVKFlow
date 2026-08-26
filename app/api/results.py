"""关键结果与运行记录的查询。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query

from app.api.common import ApiError, guard
from app.skills import runner
from app.storage import db, results, tabular

router = APIRouter(prefix="/api", tags=["results"])


@router.get("/overview")
def overview() -> dict:
    """总览页的数字。全部来自数据库，为零就是零。"""
    counts = results.overview_counts()
    return {
        "counts": counts,
        "recent_runs": runner.recent_runs(8),
        "fields": results.list_fields()[:12],
        "recent_files": db.query(
            "SELECT artifact_id, filename, ext, size, storage_mode, created_at"
            " FROM artifact WHERE kind='raw' ORDER BY created_at DESC, rowid DESC LIMIT 8"
        ),
    }


@router.get("/results")
def list_results(
    field: str | None = None, sample_id: str | None = None, quality: str | None = None,
    limit: int = Query(200, le=2000), offset: int = 0,
) -> dict:
    return results.query_results(field, sample_id, quality, limit, offset)


@router.get("/results/fields")
def fields() -> dict:
    """有哪些字段可选 —— 构效关系页选 X/Y 的候选来源。"""
    return {"fields": results.list_fields()}


@router.post("/results/{result_id}/quality")
def set_quality(result_id: int, payload: dict = Body(...)) -> dict:
    guard(results.set_quality, result_id, payload.get("quality", ""))
    return {"ok": True}


@router.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    return guard(runner.run_detail, run_id)


@router.get("/tables/{table_id}")
def table(table_id: str, limit: int = Query(2000, le=50000)) -> dict:
    return guard(tabular.read_table, table_id, limit)


@router.get("/samples")
def samples(limit: int = Query(500, le=5000)) -> dict:
    return {"samples": db.query(
        "SELECT s.*,"
        " (SELECT COUNT(*) FROM artifact a WHERE a.sample_id = s.sample_id) AS n_files,"
        " (SELECT COUNT(*) FROM key_result k WHERE k.sample_id = s.sample_id) AS n_results"
        " FROM sample s ORDER BY s.created_at DESC LIMIT ?", (limit,)
    )}


@router.get("/storage/stats")
def storage_stats() -> dict:
    """数据存储页要展示的真实分布。"""
    return {
        "by_mode": db.query(
            "SELECT storage_mode, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes"
            " FROM artifact WHERE kind='raw' GROUP BY storage_mode"
        ),
        "by_ext": db.query(
            "SELECT ext, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes"
            " FROM artifact WHERE kind='raw' GROUP BY ext ORDER BY n DESC LIMIT 12"
        ),
        "tables": db.query(
            "SELECT t.table_id, t.name, t.n_rows, t.path, r.skill_id"
            " FROM data_table t LEFT JOIN analysis_run r"
            "   ON r.analysis_run_id = t.analysis_run_id"
            " ORDER BY t.created_at DESC LIMIT 20"
        ),
        "fields": results.list_fields(),
        "counts": results.overview_counts(),
    }
