"""目录类的只读查询：最近的文件、样品清单、存储分布、导入批次的筛选面板、带光谱矩阵的样品清单、样品简介。

原来这些 SQL 直接写在路由里（app/api/results.py、files.py、spectra.py，C074 挪出来）：路由只管参数和 HTTP，
查询放存储层，别的入口（智能体、命令行）也能直接用。
"""
from __future__ import annotations

import json

from app.errors import NotFound
from app.storage import artifacts, db, ingest, results


def recent_files(limit: int = 8) -> list[dict]:
    return db.query(
        "SELECT artifact_id, filename, ext, size, storage_mode, created_at"
        " FROM artifact WHERE kind='raw' ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))


def list_samples(limit: int = 500) -> list[dict]:
    """样品清单，带每个样品有几个文件、几个关键结果（当前值）。"""
    return db.query(
        "SELECT s.*,"
        " (SELECT COUNT(*) FROM artifact a WHERE a.sample_id = s.sample_id) AS n_files,"
        # 当前值的条数 = 有没作废值的字段数（同 key_result_current 的口径，C078）
        " (SELECT COUNT(DISTINCT k.field_name) FROM key_result k"
        "   WHERE k.sample_id = s.sample_id AND k.quality != 'reject') AS n_results"
        " FROM sample s ORDER BY s.created_at DESC LIMIT ?", (limit,))


def sample_brief(sample_id: str | None) -> dict | None:
    if not sample_id:
        return None
    return db.query_one("SELECT sample_id, name, batch FROM sample WHERE sample_id = ?", (sample_id,))


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


def file_facets() -> dict:
    """文件列表的筛选面板：扩展名、导入批次、样品。"""
    return {
        "extensions": artifacts.extension_facets(),
        "batches": db.query(
            "SELECT batch_id, source_hint, file_count, created_at FROM import_batch"
            " ORDER BY created_at DESC LIMIT 30"
        ),
        "samples": db.query(
            "SELECT s.sample_id, s.name, s.batch,"
            " (SELECT COUNT(*) FROM artifact a WHERE a.sample_id = s.sample_id) AS n_files"
            " FROM sample s ORDER BY s.name LIMIT 500"
        ),
    }


def matrix_samples() -> dict:
    """带光谱矩阵的样品清单 —— 「吸收光谱 & 膜厚」的入口列表。

    判断一个文件是不是光谱矩阵只读文件头（列数 ≥ 8），不整个解析。
    判断结果存在 artifact.is_matrix 列里（导入时判一次，老数据第一次列出时惰性补判），第二次就不用再看文件了。
    """
    rows = db.query(
        "SELECT a.artifact_id, a.filename, a.display_path, a.ext, a.size, a.status,"
        "       a.meta_json, a.is_matrix, a.sample_id, s.name AS sample_name, s.batch"
        " FROM artifact a JOIN sample s ON s.sample_id = a.sample_id"
        " WHERE a.kind='raw' AND a.ext IN ('.csv','.txt','.dat','.tsv','.asc',"
        "                                  '.xlsx','.xls','.xlsm')"
        " ORDER BY s.name, a.size DESC"
    )

    by_sample: dict[str, dict] = {}
    for r in rows:
        try:
            meta = json.loads(r.get("meta_json") or "{}")
        except ValueError:
            meta = {}

        flag = r.get("is_matrix")
        if flag is None:
            flag = meta.get("matrix_like")
        if flag is None and r["status"] == "ok":
            # 导入时还没判过的老数据：现在补判。和导入时是同一份判断（ingest.mark_matrix）
            flag, meta["columns_hint"] = _probe_lazily(r["artifact_id"])

        entry = by_sample.setdefault(r["sample_id"], {
            "sample_id": r["sample_id"], "name": r["sample_name"],
            "batch": r["batch"], "matrices": [], "other_files": 0,
        })
        if flag:
            entry["matrices"].append({
                "artifact_id": r["artifact_id"], "filename": r["filename"],
                "display_path": r["display_path"], "size": r["size"],
                "status": r["status"], "columns_hint": meta.get("columns_hint"),
            })
        else:
            entry["other_files"] += 1

    out = [v for v in by_sample.values()]
    out.sort(key=lambda v: (not v["matrices"], v["name"]))
    return {
        "samples": out,
        "with_matrix": sum(1 for v in out if v["matrices"]),
        "total": len(out),
    }


def _probe_lazily(artifact_id: str) -> tuple[bool, int | None]:
    try:
        path = artifacts.local_path(artifact_id)
    except (NotFound, FileNotFoundError):
        return False, None
    pr = ingest.mark_matrix(artifact_id, path, if_unknown=False)
    return bool(pr.is_matrix), pr.columns
