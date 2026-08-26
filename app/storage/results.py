"""关键结果的读写。

key_result 是长表：一行一个字段值。好处是新增测量字段不用改 schema，
构效关系页后期直接按 field_name 透视成 X/Y。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from app.storage import db


def start_run(
    skill_id: str,
    skill_version: str,
    skill_name: str = "",
    params: dict | None = None,
    inputs: Iterable[str] = (),
    sample_id: str | None = None,
    measurement_id: str | None = None,
    source: str = "skill",
) -> str:
    run_id = db.new_id("run")
    with db.tx() as c:
        c.execute(
            "INSERT INTO analysis_run(analysis_run_id, skill_id, skill_version, skill_name,"
            " params_json, input_json, sample_id, measurement_id, status, source, started_at)"
            " VALUES(?,?,?,?,?,?,?,?,'running',?,?)",
            (run_id, skill_id, skill_version, skill_name or skill_id,
             json.dumps(params or {}, ensure_ascii=False),
             json.dumps(list(inputs), ensure_ascii=False),
             sample_id, measurement_id, source, db.now()),
        )
    return run_id


def finish_run(
    run_id: str,
    status: str,
    warnings: Iterable[str] = (),
    error: str | None = None,
    log: str = "",
) -> None:
    with db.tx() as c:
        c.execute(
            "UPDATE analysis_run SET status=?, warnings_json=?, error=?, log=?, finished_at=?"
            " WHERE analysis_run_id=?",
            (status, json.dumps(list(warnings), ensure_ascii=False), error, log[:20000],
             db.now(), run_id),
        )


def write_results(
    analysis_run_id: str,
    metrics: Iterable[dict],
    sample_id: str | None = None,
    measurement_id: str | None = None,
    source: str = "skill",
    version: str = "",
    artifact_uri: str | None = None,
) -> int:
    """写一批关键结果。数值进 value_num，文本进 value_text，两者互斥。"""
    rows = []
    for m in metrics:
        value = m.get("value")
        num, text = None, None
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            num = float(value)
        elif value is not None:
            text = str(value)
        rows.append((
            sample_id, measurement_id, analysis_run_id,
            m["field_name"], m.get("label") or m["field_name"],
            num, text, m.get("unit", "") or "",
            m.get("source") or source,
            m.get("quality") or "review",
            m.get("version") or version or None,
            m.get("artifact_uri") or artifact_uri,
            db.now(),
        ))
    if not rows:
        return 0
    with db.tx() as c:
        c.executemany(
            "INSERT INTO key_result(sample_id, measurement_id, analysis_run_id, field_name, label,"
            " value_num, value_text, unit, source, quality, version, artifact_uri, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def results_for_run(analysis_run_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM key_result WHERE analysis_run_id = ? ORDER BY id", (analysis_run_id,)
    )


def set_quality(result_id: int, quality: str) -> None:
    if quality not in ("validated", "review", "reject"):
        raise ValueError(f"未知的质量状态：{quality}")
    with db.tx() as c:
        c.execute("UPDATE key_result SET quality=? WHERE id=?", (quality, result_id))


def list_fields() -> list[dict]:
    """有哪些字段可用——构效关系页选 X/Y 时的候选来源。"""
    return db.query(
        "SELECT field_name, label, unit,"
        "       COUNT(*) AS n,"
        "       SUM(CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) AS n_numeric,"
        "       MIN(value_num) AS min_v, MAX(value_num) AS max_v"
        " FROM key_result GROUP BY field_name, unit ORDER BY n DESC"
    )


def query_results(
    field: str | None = None,
    sample_id: str | None = None,
    quality: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    where, params = [], []
    if field:
        where.append("k.field_name = ?")
        params.append(field)
    if sample_id:
        where.append("k.sample_id = ?")
        params.append(sample_id)
    if quality:
        where.append("k.quality = ?")
        params.append(quality)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = db.scalar(f"SELECT COUNT(*) FROM key_result k {clause}", tuple(params)) or 0
    rows = db.query(
        f"SELECT k.*, s.name AS sample_name, r.skill_id, r.skill_version"
        f" FROM key_result k"
        f" LEFT JOIN sample s ON s.sample_id = k.sample_id"
        f" LEFT JOIN analysis_run r ON r.analysis_run_id = k.analysis_run_id"
        f" {clause} ORDER BY k.id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


def overview_counts() -> dict[str, int]:
    """总览页的真实计数。为零就是零，不编数字。"""
    return {
        "samples": db.scalar("SELECT COUNT(*) FROM sample") or 0,
        "artifacts": db.scalar("SELECT COUNT(*) FROM artifact WHERE kind='raw'") or 0,
        "results": db.scalar("SELECT COUNT(*) FROM key_result") or 0,
        "pending_review": db.scalar("SELECT COUNT(*) FROM key_result WHERE quality='review'") or 0,
        "runs": db.scalar("SELECT COUNT(*) FROM analysis_run") or 0,
        "failed_runs": db.scalar("SELECT COUNT(*) FROM analysis_run WHERE status='failed'") or 0,
        "missing_files": db.scalar("SELECT COUNT(*) FROM artifact WHERE status='missing'") or 0,
    }
