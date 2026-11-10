"""关键结果的读写。

key_result 是长表：一行一个字段值。好处是新增测量字段不用改 schema，
构效关系页后期直接按 field_name 透视成 X/Y。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from app.storage import db
from app.errors import NotFound


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


QUALITY = ("validated", "review", "reject")


def set_quality(result_id: int, quality: str, note: str = "") -> dict:
    """审核一条关键结果：可信 / 待查 / 作废，留下理由和时间。

    返回这条结果，以及审核之后这个 (样品, 字段) 的**当前值** —— 作废了最新的那条，
    当前值退回上一次没作废的（key_result_current 的定义），界面要能告诉你退回到了哪个数。
    """
    if quality not in QUALITY:
        raise ValueError(f"未知的质量状态：{quality}（只能是 {' / '.join(QUALITY)}）")
    row = db.query_one("SELECT id, sample_id, field_name FROM key_result WHERE id = ?", (result_id,))
    if not row:
        raise NotFound(f"没有这条结果：{result_id}")
    with db.tx() as c:
        c.execute("UPDATE key_result SET quality = ?, quality_note = ?, quality_at = ? WHERE id = ?",
                  (quality, (note or "").strip()[:500] or None, db.now(), result_id))
    return {"result": _result_row(result_id),
            "current": _current(row["sample_id"], row["field_name"])}


def set_quality_many(ids: list[int], quality: str, note: str = "") -> dict:
    """一次审核几条（比如「这一次测量的结果全部作废」）。全部存在才动手，一条不存在就一条都不改。"""
    if quality not in QUALITY:
        raise ValueError(f"未知的质量状态：{quality}（只能是 {' / '.join(QUALITY)}）")
    ids = [int(i) for i in ids]
    if not ids:
        raise ValueError("没有指定要审核的结果")
    found = {r["id"] for r in db.query(
        f"SELECT id FROM key_result WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))}
    if missing := [i for i in ids if i not in found]:
        raise NotFound(f"没有这些结果：{missing[:10]}")
    with db.tx() as c:
        c.executemany("UPDATE key_result SET quality = ?, quality_note = ?, quality_at = ? WHERE id = ?",
                      [(quality, (note or "").strip()[:500] or None, db.now(), i) for i in ids])
    return {"updated": len(ids)}


_RESULT_SQL = """SELECT k.id, k.sample_id, k.field_name, k.label, k.value_num, k.value_text, k.unit,
       k.source, k.quality, k.quality_note, k.quality_at, k.version, k.created_at,
       k.analysis_run_id, r.started_at AS run_started_at, r.parent_run_id,
       json_extract(p.params_json, '$.title') AS run_title
  FROM key_result k
  LEFT JOIN analysis_run r ON r.analysis_run_id = k.analysis_run_id
  LEFT JOIN analysis_run p ON p.analysis_run_id = r.parent_run_id"""


def _result_row(result_id: int) -> dict | None:
    return db.query_one(_RESULT_SQL + " WHERE k.id = ?", (result_id,))


def _current(sample_id: str, field_name: str) -> dict | None:
    row = db.query_one("SELECT id FROM key_result_current WHERE sample_id IS ? AND field_name = ?",
                       (sample_id, field_name))
    return _result_row(row["id"]) if row else None


def sample_results(sample_id: str) -> list[dict]:
    """一个样品的全部关键结果，按字段分组：当前值 + 历史（新的在前，含作废的）。样品页的审核表用它。"""
    rows = db.query(_RESULT_SQL + " WHERE k.sample_id = ? ORDER BY k.field_name, k.id DESC",
                    (sample_id,))
    current = {r["id"] for r in db.query(
        "SELECT id FROM key_result_current WHERE sample_id = ?", (sample_id,))}
    fields: dict[str, dict] = {}
    for r in rows:
        f = fields.setdefault(r["field_name"], {
            "field_name": r["field_name"], "label": r["label"], "unit": r["unit"],
            "current": None, "history": []})
        r["is_current"] = r["id"] in current
        if r["is_current"]:
            f["current"] = r
        f["history"].append(r)
    return list(fields.values())


def list_fields() -> list[dict]:
    """有哪些字段可用——构效关系页选 X/Y 时的候选来源，总览 / 存储页的字段表。

    **一个字段一行。** 原来按（字段, 单位）分组：同一个字段换了单位就列两行，
    「N 个不同字段」也跟着多算；中文名是从组里随便挑的一行（自查 §6 #28）。
    现在：`units` 按出现次数从多到少；不止一个单位时 `mixed_units` 为真 —— 这时的
    min / max 是把不同单位的数混在一起算的，界面要提醒，不能当真。
    `label` 取出现最多的那个（没写中文名时它和字段名一样，那就给空串）。
    """
    rows = db.query(
        "SELECT field_name, label, unit,"
        "       COUNT(*) AS n,"
        "       SUM(CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) AS n_numeric,"
        "       MIN(value_num) AS min_v, MAX(value_num) AS max_v"
        " FROM key_result_current GROUP BY field_name, unit, label"
    )
    merged: dict[str, dict] = {}
    for r in rows:
        f = merged.setdefault(r["field_name"], {
            "field_name": r["field_name"], "n": 0, "n_numeric": 0,
            "min_v": None, "max_v": None, "_units": {}, "_labels": {}})
        f["n"] += r["n"]
        f["n_numeric"] += r["n_numeric"] or 0
        for key, pick in (("min_v", min), ("max_v", max)):
            if r[key] is not None:
                f[key] = r[key] if f[key] is None else pick(f[key], r[key])
        if r["unit"]:
            f["_units"][r["unit"]] = f["_units"].get(r["unit"], 0) + r["n"]
        if r["label"] and r["label"] != r["field_name"]:
            f["_labels"][r["label"]] = f["_labels"].get(r["label"], 0) + r["n"]

    out = []
    for f in merged.values():
        units = sorted(f.pop("_units").items(), key=lambda kv: (-kv[1], kv[0]))
        labels = sorted(f.pop("_labels").items(), key=lambda kv: (-kv[1], kv[0]))
        f["units"] = [u for u, _ in units]
        f["unit"] = " / ".join(f["units"])
        f["mixed_units"] = len(units) > 1
        f["label"] = labels[0][0] if labels else ""
        out.append(f)
    out.sort(key=lambda f: (-f["n"], f["field_name"]))
    return out


def query_results(
    field: str | None = None,
    sample_id: str | None = None,
    quality: str | None = None,
    limit: int = 200,
    offset: int = 0,
    history: bool = False,
) -> dict[str, Any]:
    """默认只给每个 (样品, 字段) 的当前值；history=True 才把每一次都列出来。

    构效关系拿它做 X / Y —— 同一个样品跑过三次批处理，散点图上不该出现三个点。
    """
    table = "key_result" if history else "key_result_current"
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

    total = db.scalar(f"SELECT COUNT(*) FROM {table} k {clause}", tuple(params)) or 0
    rows = db.query(
        f"SELECT k.*, s.name AS sample_name, r.skill_id, r.skill_version"
        f" FROM {table} k"
        f" LEFT JOIN sample s ON s.sample_id = k.sample_id"
        f" LEFT JOIN analysis_run r ON r.analysis_run_id = k.analysis_run_id"
        f" {clause} ORDER BY k.id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return {"total": total, "rows": rows, "limit": limit, "offset": offset,
            "history": history}


def overview_counts() -> dict[str, int]:
    """总览页的真实计数。为零就是零，不编数字。"""
    return {
        "samples": db.scalar("SELECT COUNT(*) FROM sample") or 0,
        "artifacts": db.scalar("SELECT COUNT(*) FROM artifact WHERE kind='raw'") or 0,
        "results": db.scalar("SELECT COUNT(*) FROM key_result_current") or 0,
        "pending_review": db.scalar(
            "SELECT COUNT(*) FROM key_result_current WHERE quality='review'") or 0,
        "runs": db.scalar("SELECT COUNT(*) FROM analysis_run") or 0,
        "failed_runs": db.scalar("SELECT COUNT(*) FROM analysis_run WHERE status='failed'") or 0,
        "missing_files": db.scalar("SELECT COUNT(*) FROM artifact WHERE status='missing'") or 0,
    }


def run_detail(run_id: str) -> dict:
    """一次运行的全部：参数、结果、数值表、派生图。"""
    from app.storage import tabular

    run = db.query_one("SELECT * FROM analysis_run WHERE analysis_run_id = ?", (run_id,))
    if not run:
        raise NotFound(f"没有这次运行记录：{run_id}")
    return {
        "run": run,
        "results": results_for_run(run_id),
        "tables": tabular.tables_for_run(run_id),
        "figures": db.query(
            "SELECT artifact_id, filename, mime FROM artifact WHERE produced_by = ?", (run_id,)
        ),
    }


def recent_runs(limit: int = 50) -> list[dict]:
    return db.query(
        "SELECT r.*, s.name AS sample_name, s.batch AS sample_batch,"
        "       (SELECT COUNT(*) FROM key_result k WHERE k.analysis_run_id = r.analysis_run_id)"
        "         AS n_results"
        " FROM analysis_run r LEFT JOIN sample s ON s.sample_id = r.sample_id"
        " ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?", (limit,),
    )
