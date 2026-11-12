"""对比历史的整理：给一次批处理改名、删掉一次批处理。

原来对比历史只能加不能减 —— 试参数跑出来的十几次、跑错样品的那次都永远挂在列表里，名字也改不了（技术债 C077）。

**删的是什么**（删之前 footprint() 把这些数给你看，界面上的确认框照着说）：
  - 这次批处理的父运行和每个样品的子运行
  - 它们写的关键结果（key_result）—— 这些样品的「当前值」自动退回上一次批处理算的（当前值是视图，见 schema.sql）；
    在这次结果上做的审核（可信 / 待查 / 作废和理由）一起没了
  - 长表文件（workspace/tables/ 里的曲线表、膜厚表）和登记
  - 钉在这次对比上的 AI 分析
**不动的**：原始数据、样品、别的批处理、AI 对话本身（对话的范围里记着这次的编号的话，
以后问起来 AI 拿到的是最近一次对比，并且会被告知你问的那次已经删了）。
还在跑的不能删（先停下来）。
"""
from __future__ import annotations

import json
import logging

from app import config
from app.batch import BATCH_SKILL_ID
from app.errors import Invalid, NotFound
from app.storage import db

log = logging.getLogger("hte.batch")

TITLE_MAX = 120


def _parent(run_id: str) -> dict:
    run = db.query_one("SELECT * FROM analysis_run WHERE analysis_run_id = ?"
                       " AND skill_id = ? AND parent_run_id IS NULL", (run_id, BATCH_SKILL_ID))
    if not run:
        raise NotFound(f"没有这次批处理：{run_id}")
    return run


def rename(run_id: str, title: str) -> dict:
    """改对比历史里显示的名字（存在这次运行的参数里，和提交时填的是同一个字段）。"""
    title = (title or "").strip()[:TITLE_MAX]
    if not title:
        raise Invalid("名字不能是空的", "bad_title")
    run = _parent(run_id)
    try:
        params = json.loads(run.get("params_json") or "{}")
    except json.JSONDecodeError:
        params = {}
    params["title"] = title
    with db.tx() as c:
        c.execute("UPDATE analysis_run SET params_json = ? WHERE analysis_run_id = ?",
                  (json.dumps(params, ensure_ascii=False), run_id))
    return {"ok": True, "analysis_run_id": run_id, "title": title}


def _run_ids(run_id: str) -> tuple[str, ...]:
    """父运行 + 它的每个子运行。"""
    rows = db.query("SELECT analysis_run_id FROM analysis_run WHERE parent_run_id = ?", (run_id,))
    return (run_id, *(r["analysis_run_id"] for r in rows))


def _in(ids: tuple[str, ...]) -> str:
    return ",".join("?" * len(ids))


def footprint(run_id: str) -> dict:
    """删掉这次会带走什么。确认框照这个说，不让人盲删。"""
    run = _parent(run_id)
    ids = _run_ids(run_id)
    q = _in(ids)
    n_results = db.scalar(f"SELECT COUNT(*) FROM key_result WHERE analysis_run_id IN ({q})", ids)
    return {
        "analysis_run_id": run_id,
        "status": run["status"],
        "n_samples": len(ids) - 1,
        "n_results": n_results,
        # 这些现在是样品的「当前值」：删了以后退回上一次批处理的值（没有上一次就没有值了）
        "n_current": db.scalar(f"SELECT COUNT(*) FROM key_result_current WHERE analysis_run_id IN ({q})", ids),
        "n_reviewed": db.scalar(f"SELECT COUNT(*) FROM key_result WHERE analysis_run_id IN ({q})"
                                " AND quality_at IS NOT NULL", ids),
        "n_tables": db.scalar(f"SELECT COUNT(*) FROM data_table WHERE analysis_run_id IN ({q})", ids),
        "n_pins": db.scalar("SELECT COUNT(*) FROM ai_pin WHERE analysis_run_id = ?", (run_id,)),
    }


def delete(run_id: str) -> dict:
    """删掉一次批处理（见文件头：删什么、不动什么）。返回删之前的 footprint。"""
    fp = footprint(run_id)
    if fp["status"] == "running":
        raise Invalid("这次批处理还在跑。先在结果页点「停止」，停下来再删。", "running")
    ids = _run_ids(run_id)
    q = _in(ids)
    paths = [r["path"] for r in db.query(f"SELECT path FROM data_table WHERE analysis_run_id IN ({q})", ids)]
    with db.tx() as c:
        c.execute("DELETE FROM ai_pin WHERE analysis_run_id = ?", (run_id,))
        # key_result / data_table 跟着 ON DELETE CASCADE 走（schema.sql）
        c.execute(f"DELETE FROM analysis_run WHERE analysis_run_id IN ({q})", ids)
    # 库里已经没有登记了，文件删不掉（被别的程序开着）只记日志 —— 留下的是孤儿文件，不是坏数据
    root = config.WORKSPACE.resolve()
    for rel in paths:
        p = (config.WORKSPACE / rel).resolve()
        if root not in p.parents:
            continue
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删批处理 %s 的长表文件失败：%s（%s）", run_id, rel, exc)
    return {"ok": True, "deleted": fp}
