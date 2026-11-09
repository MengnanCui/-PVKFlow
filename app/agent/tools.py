"""智能体能调的工具。

照 IFlow（lib/agent/types.ts）的规矩，每个工具带一个**风险等级**，这是整套设计里最要紧的字段：

  · read          只读。直接跑，不打断人。
  · write         会改你的数据，但改得回来（审核状态、新建样品集）。默认停下来等你点一下；
                  你可以勾「本次对话内不再询问这一类」。
  · irreversible  改了回不来（删除样品集）。**每一次都问，勾不了，记不住。**

判错的成本必须落在「多点一下」这一侧。

能碰的东西（P-49：你要它能读写文件、跑批处理、管任务）：
  · 平台的库：样品、关键结果、样品集
  · 文件：只在「能碰的文件夹」名单里（app/agent/files.py）；写之前备份、可撤销，所以算 write 不算 irreversible
  · 批处理与后台任务：提交一次批处理（会给每个样品写一套新结果）、看进度 / 等它跑完、停下来
**不跑任意命令**（没有 shell）。参数校验在这里做：模型给的参数不可信。
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent import files
from app.storage import db

READ, WRITE, IRREVERSIBLE = "read", "write", "irreversible"

# 回路在派发工具前填：这一轮属于哪个会话、用户有没有点「停止」（等任务时每秒看一眼）
_ctx = threading.local()


def session_id() -> str | None:
    return getattr(_ctx, "session_id", None)


def stop_requested() -> bool:
    f = getattr(_ctx, "should_stop", None)
    return bool(f and f())
# 一个工具结果最多给模型多少字。超了就截断并明说 —— 不然一张大表能把上下文撑爆
TOOL_MAX_CHARS = 12000


@dataclass
class Tool:
    name: str
    label: str                       # 界面上的中文名
    description: str                 # 进系统提示 / tools 定义：要短，说清**什么时候该用它**
    schema: dict
    risk: str
    handler: Callable[..., Any]
    preview: Callable[..., str] | None = None   # 审批卡上给人看的那句话（只读，不改任何东西）
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "description": self.description,
                "risk": self.risk, "schema": self.schema}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def _num(v):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v


def _round(v, n=4):
    if isinstance(v, float):
        return float(f"{v:.{n}g}") if math.isfinite(v) else None
    return v


# ------------------------------------------------------------------ 只读
def t_overview() -> dict:
    from app.batch import recent_batches
    from app.storage import results

    runs = []
    for r in recent_batches(5):
        p = json.loads(r.get("params_json") or "{}")
        runs.append({"run_id": r["analysis_run_id"], "title": p.get("title") or "",
                     "n_samples": r["n_children"], "n_failed": r["n_failed"],
                     "started_at": r["started_at"]})
    return {"counts": results.overview_counts(), "recent_batches": runs,
            "note": "pending_review = 当前值里还是「待查」的结果数；missing_files = 记录在案但找不到的原始文件"}


def t_list_fields() -> dict:
    from app.storage import results

    out = []
    for f in results.list_fields():
        out.append({"field_name": f["field_name"], "label": f.get("label") or f["field_name"],
                    "unit": f.get("unit") or "", "n_numeric": f["n_numeric"],
                    "min": _round(f.get("min_v")), "max": _round(f.get("max_v")),
                    **({"units_differ": f["units"]} if f.get("mixed_units") else {})})
    return {"fields": out, "note": "这些是每个样品「当前值」里的字段（作废的不算）"}


def t_find_samples(q: str = "", batch: str = "", limit: int = 20, offset: int = 0) -> dict:
    from app.storage import selection

    flt: dict = {}
    if q:
        flt["q"] = q
    if batch:
        flt["batch"] = [batch]
    limit = max(1, min(int(limit), 50))
    page = selection.page(flt, limit=limit, offset=max(0, int(offset)))
    rows = [{"sample_id": r["sample_id"], "name": r["name"], "batch": r["batch"],
             "measured_at": r["measured_at"], "n_results": r["n_results"]} for r in page["rows"]]
    nxt = page["offset"] + len(rows)
    return {"total": page["total"], "samples": rows,
            **({"next_offset": nxt} if nxt < page["total"] else {})}


def t_sample_results(sample_id: str) -> dict:
    from app.storage import results

    s = db.query_one("SELECT sample_id, name, batch FROM sample WHERE sample_id = ?", (sample_id,))
    if not s:
        raise KeyError(f"没有这个样品：{sample_id}（先用 find_samples 找到它的 sample_id）")
    fields = []
    for f in results.sample_results(sample_id):
        cur = f["current"]
        fields.append({
            "field_name": f["field_name"], "label": f["label"], "unit": f["unit"] or "",
            "current": None if not cur else {
                "result_id": cur["id"], "value": _num(cur["value_num"]) if cur["value_num"] is not None else cur["value_text"],
                "quality": cur["quality"], "reviewed_at": cur["quality_at"], "note": cur["quality_note"],
                "from_run": cur["run_title"] or "", "at": cur["run_started_at"] or cur["created_at"]},
            "n_history": len(f["history"]),
            "older": [{"result_id": r["id"], "value": _num(r["value_num"]), "quality": r["quality"]}
                      for r in f["history"] if not r["is_current"]][:5],
        })
    return {"sample": s, "fields": fields,
            "note": "quality：validated=可信 review=待查 reject=作废；reviewed_at 为空 = 还没有人审过"}


def t_correlate(x: str, y: str, batch: str = "") -> dict:
    from app.storage import relation

    flt = {"batch": [batch]} if batch else {}
    d = relation.pairs(x, y, flt)
    st = d["stats"]
    off: list[dict] = []
    fit = st.get("fit")
    if fit and st.get("pearson") and len(d["points"]) >= 4:
        res = [(p, p["y"] - (fit["slope"] * p["x"] + fit["intercept"])) for p in d["points"]]
        sd = math.sqrt(sum(r * r for _, r in res) / max(1, len(res) - 2)) or 1.0
        for p, r in sorted(res, key=lambda t: -abs(t[1]))[:5]:
            off.append({"sample_id": p["sample_id"], "name": p["name"], "batch": p["batch"],
                        "x": _round(p["x"]), "y": _round(p["y"]), "residual_in_sd": round(r / sd, 2),
                        "x_result_id": p["x_id"], "y_result_id": p["y_id"]})
    return {
        "x": {k: d["x"][k] for k in ("field_name", "label", "unit")},
        "y": {k: d["y"][k] for k in ("field_name", "label", "unit")},
        "n": st["n"], "n_only_x": d["n_only_x"], "n_only_y": d["n_only_y"],
        "pearson": st.get("pearson"), "spearman": st.get("spearman"), "fit": fit,
        "why_no_stats": st.get("why"),
        "units_differ": [f["field_name"] for f in (d["x"], d["y"]) if f.get("mixed_units")],
        "most_off_trend": off,
        "note": "most_off_trend = 离最小二乘直线最远的几个（残差以标准差计）；只是「值得看一眼」，不等于测错了",
    }


def t_compare_batches(field: str) -> dict:
    from app.storage import relation

    d = relation.groups(field)
    groups = [{"batch": g["name"], "n": g["n"], "median": _round(g["median"]), "q1": _round(g["q1"]),
               "q3": _round(g["q3"]), "min": _round(g["min"]), "max": _round(g["max"]),
               "outliers": [{"name": o["name"], "y": _round(o["y"]), "result_id": o["result_id"]}
                            for o in g["outliers"]]} for g in d["groups"][:60]]
    return {"field": {k: d["y"][k] for k in ("field_name", "label", "unit")}, "n": d["n"],
            "groups": groups, **({"truncated_groups": len(d["groups"])} if len(d["groups"]) > 60 else {}),
            "note": "离群 = 在 1.5 倍四分位距之外（Tukey）"}


def t_list_runs(limit: int = 10) -> dict:
    from app.batch import recent_batches

    out = []
    for r in recent_batches(max(1, min(int(limit), 30))):
        p = json.loads(r.get("params_json") or "{}")
        out.append({"run_id": r["analysis_run_id"], "title": p.get("title") or "", "status": r["status"],
                    "n_samples": r["n_children"], "n_failed": r["n_failed"], "started_at": r["started_at"]})
    return {"runs": out}


def t_list_sample_sets() -> dict:
    from app.storage import sets

    return {"sets": [{"set_id": s["set_id"], "name": s["name"], "kind": s["kind"], "count": s["count"]}
                     for s in sets.list_all()],
            "note": "kind：dynamic=存筛选式、新样品会自动进来；pinned=钉死的快照"}


def t_check_files() -> dict:
    from app.storage import ingest

    r = ingest.verify_references(limit=30)
    return {"checked": r["checked"], "missing": len(r["missing"]), "restored": len(r["restored"]),
            "missing_files": [{k: m[k] for k in ("filename", "sample_name", "path", "storage_mode")}
                              for m in r["missing_files"]],
            **({"truncated": True} if r["truncated"] else {})}


# ------------------------------------------------------------------ 会改数据的
_QUALITY_ZH = {"validated": "可信", "review": "待查", "reject": "作废"}


def _results_brief(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return db.query(
        f"SELECT k.id, k.field_name, COALESCE(k.label, k.field_name) AS label, k.value_num, k.unit,"
        f"       k.quality, s.name AS sample_name"
        f"  FROM key_result k LEFT JOIN sample s ON s.sample_id = k.sample_id"
        f" WHERE k.id IN ({marks}) ORDER BY k.id", tuple(ids))


def t_set_quality(result_ids: list, quality: str, note: str = "") -> dict:
    from app.storage import results

    ids = [int(i) for i in result_ids]
    out = results.set_quality_many(ids, quality, note)
    return {"updated": out["updated"], "quality": quality,
            "now": [{"result_id": r["id"], "sample": r["sample_name"], "field": r["field_name"],
                     "quality": r["quality"]} for r in _results_brief(ids)],
            "note": "作废的结果不再算「当前值」，这个样品的这个字段退回上一次没作废的那条"}


def p_set_quality(result_ids: list, quality: str, note: str = "") -> str:
    rows = _results_brief([int(i) for i in result_ids])
    found = {r["id"] for r in rows}
    lines = [f"把 {len(rows)} 条结果标为「{_QUALITY_ZH.get(quality, quality)}」"
             + (f"（理由：{note}）" if note else "") + "："]
    for r in rows[:12]:
        v = "—" if r["value_num"] is None else f"{r['value_num']:.4g}"
        lines.append(f"· {r['sample_name']} 的 {r['label']} = {v} {r['unit'] or ''}"
                     f"（现在是「{_QUALITY_ZH.get(r['quality'], r['quality'])}」）")
    if len(rows) > 12:
        lines.append(f"· …… 另外 {len(rows) - 12} 条")
    if missing := [i for i in result_ids if int(i) not in found]:
        lines.append(f"（这些编号不存在，整次会被拒绝：{missing[:10]}）")
    return "\n".join(lines)


def _set_filter(batch: str, q: str) -> dict:
    flt: dict = {}
    if batch:
        flt["batch"] = [batch]
    if q:
        flt["q"] = q
    return flt


def t_save_sample_set(name: str, kind: str = "pinned", sample_ids: list | None = None,
                      batch: str = "", q: str = "") -> dict:
    from app.storage import sets

    s = sets.create(name, kind, _set_filter(batch, q), sample_ids=[str(i) for i in sample_ids or []] or None)
    return {"set_id": s["set_id"], "name": s["name"], "kind": s["kind"], "count": s["count"]}


def p_save_sample_set(name: str, kind: str = "pinned", sample_ids: list | None = None,
                      batch: str = "", q: str = "") -> str:
    from app.storage import selection

    if sample_ids:
        n, how = len(sample_ids), f"指定的 {len(sample_ids)} 个样品"
    else:
        flt = _set_filter(batch, q)
        n = selection.count(flt)
        how = "、".join(x for x in (f"批次 {batch}" if batch else "", f"名字含「{q}」" if q else "") if x) or "全部样品"
        how += f"（此刻 {n} 个）"
    kind_zh = "固定集（钉死这些样品）" if kind == "pinned" else "动态集（以后新导入的符合条件的也会进来）"
    return f"新建样品集「{name}」：{kind_zh}，{how}"


def t_delete_sample_set(set_id: str) -> dict:
    from app.storage import sets

    return {"deleted": sets.delete(set_id),
            "note": "只删了样品集本身，样品和结果都还在"}


def p_delete_sample_set(set_id: str) -> str:
    from app.storage import sets

    try:
        s = sets.get(set_id)
    except KeyError:
        return f"删除样品集 {set_id}（这个编号不存在，会失败）"
    return (f"删除样品集「{s['name']}」（{'固定集' if s['kind'] == 'pinned' else '动态集'}，"
            f"{s['count']} 个样品）。删了就找不回这份{'快照' if s['kind'] == 'pinned' else '筛选式'}；"
            "样品和结果不动。")


# ------------------------------------------------------------------ 文件
def t_list_files(path: str = ".", depth: int = 1, show_all: bool = False) -> dict:
    return files.list_dir(path, depth, show_all)


def t_read_file(path: str, offset: int = 0, limit: int = 400) -> dict:
    return files.read_text(path, offset, limit)


def t_search_files(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False, max: int = 100) -> dict:
    return files.grep(pattern, path, glob, ignore_case, max)


def t_write_file(path: str, content: str | None = None, find: str | None = None, replace: str | None = None,
                 note: str = "") -> dict:
    return files.write(path, content, find, replace, note, session_id())


def p_write_file(path: str, content: str | None = None, find: str | None = None, replace: str | None = None,
                 note: str = "") -> str:
    return files.preview_write(path, content, find, replace, note)


def t_file_changes(path: str = "", this_session_only: bool = True) -> dict:
    return {"changes": files.changes(session_id() if this_session_only else None, path or None)}


def t_rollback_file(change_id: str = "", all_in_this_session: bool = False, force: bool = False) -> dict:
    if not change_id and not all_in_this_session:
        raise ValueError("给 change_id，或者 all_in_this_session=true")
    return files.rollback(change_id or None, None if change_id else session_id(), force)


def p_rollback_file(change_id: str = "", all_in_this_session: bool = False, force: bool = False) -> str:
    rows = files.changes(session_id()) if not change_id else [c for c in files.changes(None, None, 200)
                                                               if c["change_id"] == change_id]
    rows = [r for r in rows if not r["rolled_back"]]
    if not rows:
        return "撤销文件改动（没找到可撤销的改动，会失败）"
    lines = [f"撤销 {len(rows)} 处文件改动（从备份还原；新建的文件会被删掉）{'，强制' if force else ''}："]
    lines += [f"· {r['path']}（{r['at']}{'，当时是新建' if r['created'] else ''}）" for r in rows[:12]]
    return "\n".join(lines)


# ------------------------------------------------------------------ 批处理与任务
def _batch_filter(sample_ids: list | None, batch: str, q: str, all_samples: bool) -> dict:
    flt: dict = {}
    if sample_ids:
        flt["ids"] = [str(i) for i in sample_ids]
    if batch:
        flt["batch"] = [batch]
    if q:
        flt["q"] = q
    if not flt and not all_samples:
        raise ValueError("要指定样品范围：sample_ids / batch / q / set_id，或者 all_samples=true（全部样品）")
    return flt


def t_run_batch(sample_ids: list | None = None, batch: str = "", q: str = "", set_id: str = "",
                all_samples: bool = False, title: str = "", recipe: dict | None = None) -> dict:
    from app import batch as batch_mod

    flt = None if set_id else _batch_filter(sample_ids, batch, q, all_samples)
    out = batch_mod.submit_batch(flt, set_id=set_id or None, recipe=recipe, title=title)
    t = out["task"]
    return {"task_id": t["task_id"], "title": t["title"], "n_samples": out["n_samples"], "recipe": out["recipe"],
            "next": f"用 get_task(task_id=\"{t['task_id']}\", wait_seconds=60) 等它跑完，再用 batch_summary 看结果"}


def _recipe_diff(recipe: dict | None) -> str:
    from app import batch as batch_mod

    base = batch_mod.Recipe.from_dict(None).as_dict()
    mine = batch_mod.Recipe.from_dict(recipe).as_dict()
    changed = [f"{k}：{base[k]} → {v}" for k, v in mine.items() if k != "module_params" and base.get(k) != v]
    if mine.get("module_params") != base.get("module_params"):
        changed.append(f"模块参数：{json.dumps(mine['module_params'], ensure_ascii=False)}")
    if not changed:
        return (f"配方：默认（膜厚窗口 {base['band_min']}–{base['band_max']} nm，积分 {base['integral_min']}–"
                f"{base['integral_max']} nm，斜率 {base['slope_center']} ± {base['slope_half_width']} nm）")
    return "配方改了：" + "；".join(changed)


def p_run_batch(sample_ids: list | None = None, batch: str = "", q: str = "", set_id: str = "",
                all_samples: bool = False, title: str = "", recipe: dict | None = None) -> str:
    from app.storage import selection, sets

    if set_id:
        s = sets.get(set_id)
        flt, scope = sets.resolve(set_id), f"样品集「{s['name']}」"
    else:
        flt = _batch_filter(sample_ids, batch, q, all_samples)
        scope = "、".join(x for x in (f"指定的 {len(sample_ids)} 个样品" if sample_ids else "",
                                      f"批次 {batch}" if batch else "", f"名字含「{q}」" if q else "") if x) or "全部样品"
    n = selection.count({**selection.normalize(flt), "has_matrix": True})
    return (f"对 {n} 个样品跑一次批处理（{scope}）{f'，标题「{title}」' if title else ''}。\n{_recipe_diff(recipe)}\n"
            "跑完每个样品会多一套关键结果，成为「当前值」（之前的留作历史，可以在样品页审核 / 作废）。"
            + ("\n（一个带光谱矩阵的样品都没命中，批了也会失败）" if not n else ""))


def _task_brief(t: dict) -> dict:
    res = t.get("result") or {}
    out = {"task_id": t["task_id"], "title": t.get("title"), "status": t["status"], "done": t["done"],
           "progress": t.get("progress"), "total": t.get("total"), "percent": t.get("percent"),
           "n_ok": t.get("n_ok"), "n_failed": t.get("n_failed"), "message": t.get("message"),
           "created_at": t.get("created_at"), "finished_at": t.get("finished_at")}
    if res.get("parent_run_id"):
        out["run_id"] = res["parent_run_id"]
    if t.get("error"):
        out["error"] = str(t["error"]).splitlines()[0][:300]
    return out


TASK_WAIT_MAX = 60


def t_get_task(task_id: str, wait_seconds: int = 0) -> dict:
    """看一个后台任务；wait_seconds > 0 时最多等这么久（每秒看一眼，跑完就回；用户点了停止也立刻回）。"""
    from app import tasks

    wait = max(0, min(int(wait_seconds), TASK_WAIT_MAX))
    t0 = time.monotonic()
    t = tasks.get(task_id)
    while not t["done"] and time.monotonic() - t0 < wait and not stop_requested():
        time.sleep(1.0)
        t = tasks.get(task_id)
    out = _task_brief(t)
    out["waited_s"] = round(time.monotonic() - t0, 1)
    if not t["done"]:
        out["note"] = "还在跑。可以再调 get_task 接着等，或者先把任务 id 告诉用户、让他在数据处理页 / 对比页看进度"
    elif out.get("run_id"):
        out["next"] = f"用 batch_summary(run_id=\"{out['run_id']}\") 看这次的结果"
    return out


def t_list_tasks(limit: int = 10) -> dict:
    from app import tasks

    return {"tasks": [_task_brief(t) for t in tasks.recent(max(1, min(int(limit), 30)))]}


def t_cancel_task(task_id: str) -> dict:
    from app import tasks

    return {**_task_brief(tasks.cancel(task_id)), "note": "取消是「停在这儿」：已经跑完的样品结果保留"}


def p_cancel_task(task_id: str) -> str:
    from app import tasks

    try:
        t = tasks.get(task_id)
    except KeyError:
        return f"停下任务 {task_id}（这个任务不存在，会失败）"
    if t["done"]:
        return f"任务「{t['title']}」已经结束了（{t['status']}），不用停"
    return (f"停下任务「{t['title']}」（现在 {t.get('progress') or 0} / {t.get('total') or '?'}）。"
            "已经跑完的样品结果保留，没跑的不跑了。")


def t_batch_summary(run_id: str) -> dict:
    from app import batch as batch_mod

    d = batch_mod.batch_detail(run_id)
    kids = [c["analysis_run_id"] for c in d["children"]]
    stats: dict[str, dict] = {}
    if kids:
        rows = db.query(
            f"SELECT field_name, COALESCE(label, field_name) AS label, unit, value_num FROM key_result"
            f" WHERE analysis_run_id IN ({','.join('?' * len(kids))}) AND value_num IS NOT NULL", tuple(kids))
        for r in rows:
            f = stats.setdefault(r["field_name"], {"label": r["label"], "unit": r["unit"] or "", "v": []})
            if r["value_num"] is not None and math.isfinite(r["value_num"]):
                f["v"].append(r["value_num"])
    fields = [{"field_name": k, "label": f["label"], "unit": f["unit"], "n": len(f["v"]),
               "mean": _round(statistics.fmean(f["v"])), "median": _round(statistics.median(f["v"])),
               "min": _round(min(f["v"])), "max": _round(max(f["v"]))}
              for k, f in sorted(stats.items()) if f["v"]]
    failed = [{"sample": c["sample_name"], "error": (c.get("error") or "").splitlines()[0][:200] if c.get("error") else ""}
              for c in d["children"] if c["status"] == "failed"]
    run = d["run"]
    return {"run_id": run_id, "title": (run.get("params") or {}).get("title") or "", "status": run.get("status"),
            "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
            "n_samples": len(d["children"]), "n_ok": d["n_ok"], "n_failed": d["n_failed"],
            "failed": failed[:20], "fields": fields,
            "note": "fields 是这一次算出来的数（不是当前值）；同一个样品别的批处理算的不在这里"}


# ------------------------------------------------------------------ 注册表
_S = {"type": "string"}
TOOLS: list[Tool] = [
    Tool("overview", "平台概况",
         "平台里有多少样品、原始文件、关键结果，几条还待审核、几个文件找不到，最近几次批处理。问「现在有什么数据」时先调它。",
         _obj({}), READ, t_overview),
    Tool("list_fields", "字段清单",
         "全部可分析的数值字段：字段名、中文名、单位、有几个样品有值、范围。不知道字段名（field_name）时先调它，别猜。",
         _obj({}), READ, t_list_fields),
    Tool("find_samples", "找样品",
         "按名字关键词 / 批次找样品，返回 sample_id、名字、批次、有几个结果。结果多时用 offset 翻页（返回里有 next_offset）。",
         _obj({"q": {**_S, "description": "名字里包含的文字"},
               "batch": {**_S, "description": "批次（样品号），如 ZG0013"},
               "limit": {"type": "integer", "description": "最多返回几个，≤ 50，默认 20"},
               "offset": {"type": "integer", "description": "从第几个开始"}}), READ, t_find_samples),
    Tool("sample_results", "样品的关键结果",
         "一个样品每个字段的当前值、审核状态（可信 / 待查 / 作废）、result_id，以及以前算过的几次。要审核某个结果时先用它拿 result_id。",
         _obj({"sample_id": {**_S, "description": "样品编号（smp_ 开头）"}}, ["sample_id"]), READ, t_sample_results),
    Tool("correlate", "两个字段的相关",
         "两个字段在样品之间的相关：皮尔逊 r（含 p 值、95% 区间）、斯皮尔曼 ρ、拟合直线，以及离趋势最远的几个样品。数值一律以它算的为准，不要自己估。",
         _obj({"x": {**_S, "description": "X 的 field_name"}, "y": {**_S, "description": "Y 的 field_name"},
               "batch": {**_S, "description": "只看这个批次（可选）"}}, ["x", "y"]), READ, t_correlate),
    Tool("compare_batches", "按批次比较",
         "一个字段按批次分组的分布：每批样品数、中位数、四分位、范围、离群的样品（带 result_id）。",
         _obj({"field": {**_S, "description": "field_name"}}, ["field"]), READ, t_compare_batches),
    Tool("list_runs", "最近的批处理",
         "最近几次批处理：编号、标题、几个样品、几个失败、什么时候跑的。",
         _obj({"limit": {"type": "integer", "description": "≤ 30，默认 10"}}), READ, t_list_runs),
    Tool("list_sample_sets", "样品集清单",
         "已经存下的样品集：set_id、名字、动态 / 固定、几个样品。",
         _obj({}), READ, t_list_sample_sets),
    Tool("check_files", "检查原始文件",
         "查一遍原始文件还在不在记录的位置，找不到的列出文件名、样品、路径。只同步「找不到」标记，不改任何数据。",
         _obj({}), READ, t_check_files),
    Tool("set_quality", "审核结果",
         "把一条或几条关键结果标为 validated（可信）/ review（待查）/ reject（作废），可附理由。作废后这个样品这个字段的当前值退回上一次。会先问用户。",
         _obj({"result_ids": {"type": "array", "items": {"type": "integer"}, "description": "result_id 列表"},
               "quality": {"type": "string", "enum": ["validated", "review", "reject"]},
               "note": {**_S, "description": "理由（作废时尽量写）"}}, ["result_ids", "quality"]),
         WRITE, t_set_quality, p_set_quality),
    Tool("save_sample_set", "存样品集",
         "把一批样品存成样品集。kind=pinned 钉死这些样品（给 sample_ids，或给 batch / q 按此刻命中的钉）；kind=dynamic 存筛选式（batch / q）。会先问用户。",
         _obj({"name": _S, "kind": {"type": "string", "enum": ["pinned", "dynamic"]},
               "sample_ids": {"type": "array", "items": _S}, "batch": _S, "q": _S}, ["name"]),
         WRITE, t_save_sample_set, p_save_sample_set),
    Tool("delete_sample_set", "删除样品集",
         "删除一个样品集（只删集，不删样品）。删了找不回来，每次都会问用户。",
         _obj({"set_id": _S}, ["set_id"]), IRREVERSIBLE, t_delete_sample_set, p_delete_sample_set),
    # ---------------------------------------------------------------- 文件
    Tool("list_files", "列文件",
         "列「能碰的文件夹」里的文件和子文件夹。path 相对工作区，或写名单里文件夹的完整路径；depth 1–4。不知道文件在哪先调它，别凭印象编路径。",
         _obj({"path": {**_S, "description": "默认是工作区根目录"},
               "depth": {"type": "integer", "description": "展开几层，1–4，默认 1"},
               "show_all": {"type": "boolean", "description": "也列 cache / __pycache__ 这类默认跳过的"}}), READ, t_list_files),
    Tool("read_file", "读文件",
         "读一个文本文件（带行号；UTF-8 / UTF-16 / GBK 都认）。大文件用 offset / limit 分段读，返回 next_offset 就是没读完。密钥会被打码。",
         _obj({"path": _S, "offset": {"type": "integer", "description": "从第几行开始（0 起）"},
               "limit": {"type": "integer", "description": "读几行，默认 400，最多 2000"}}, ["path"]), READ, t_read_file),
    Tool("search_files", "搜文件内容",
         "在文件夹里按正则搜文件内容，返回 文件:行号:内容。glob 限定文件名，如 *.csv、**/Options.json。",
         _obj({"pattern": {**_S, "description": "正则"}, "path": _S, "glob": _S,
               "ignore_case": {"type": "boolean"}, "max": {"type": "integer", "description": "最多几条，默认 100"}},
              ["pattern"]), READ, t_search_files),
    Tool("write_file", "写文件",
         "新建或修改文件：给 content = 整个文件；或给 find + replace = 把唯一出现的一段原文换掉（先 read_file 拿逐字原文）。"
         "每次写之前自动备份，返回 change_id，可以 rollback_file 撤销。工作区里的 raw / config / modules / 数据库是只读的。会先问用户。",
         _obj({"path": _S, "content": {**_S, "description": "整个文件的新内容（和 find/replace 二选一）"},
               "find": {**_S, "description": "要替换的原文（逐字一致、只出现一次）"}, "replace": _S,
               "note": {**_S, "description": "一句话说明为什么改"}}, ["path"]), WRITE, t_write_file, p_write_file),
    Tool("file_changes", "文件改动记录",
         "智能体改过的文件（change_id、路径、时间、是否已撤销）。默认只看这次对话的。",
         _obj({"path": _S, "this_session_only": {"type": "boolean", "description": "默认 true"}}), READ, t_file_changes),
    Tool("rollback_file", "撤销文件改动",
         "从备份还原智能体改过的文件：给 change_id 撤销一次；all_in_this_session=true 撤销这次对话的全部改动。文件之后又被改过时默认拒绝。会先问用户。",
         _obj({"change_id": _S, "all_in_this_session": {"type": "boolean"}, "force": {"type": "boolean"}}),
         WRITE, t_rollback_file, p_rollback_file),
    # ---------------------------------------------------------------- 批处理与任务
    Tool("run_batch", "跑批处理",
         "提交一次批处理（后台任务）：范围给 sample_ids / batch / q / set_id 之一，或 all_samples=true；recipe 不给就用默认配方。"
         "每个样品会多一套关键结果并成为当前值。会先问用户。提交后用 get_task 等它跑完。",
         _obj({"sample_ids": {"type": "array", "items": _S}, "batch": {**_S, "description": "批次（样品号）"},
               "q": {**_S, "description": "名字关键词"}, "set_id": _S,
               "all_samples": {"type": "boolean"}, "title": {**_S, "description": "这次批处理叫什么（对比历史里看得到）"},
               "recipe": {"type": "object", "description": "配方，如 {\"band_min\": 780, \"band_max\": 1100}；不给 = 默认"}}),
         WRITE, t_run_batch, p_run_batch),
    Tool("get_task", "看任务进度",
         "看一个后台任务（批处理）的状态与进度；wait_seconds > 0（最多 60）时等它跑完再回。跑完了会给 run_id。",
         _obj({"task_id": _S, "wait_seconds": {"type": "integer", "description": "最多等几秒，0–60"}}, ["task_id"]),
         READ, t_get_task),
    Tool("list_tasks", "任务列表",
         "最近的后台任务：id、标题、状态、进度、成功 / 失败几个。",
         _obj({"limit": {"type": "integer", "description": "≤ 30，默认 10"}}), READ, t_list_tasks),
    Tool("cancel_task", "停下任务",
         "停下一个正在跑的后台任务。已经跑完的样品结果保留。会先问用户。",
         _obj({"task_id": _S}, ["task_id"]), WRITE, t_cancel_task, p_cancel_task),
    Tool("batch_summary", "批处理结果汇总",
         "一次批处理的汇总：几个样品成功 / 失败、失败的原因、每个字段这一次算出来的均值 / 中位数 / 范围。run_id 来自 get_task 或 list_runs。",
         _obj({"run_id": _S}, ["run_id"]), READ, t_batch_summary),
]
BY_NAME = {t.name: t for t in TOOLS}


# ------------------------------------------------------------------ 校验与派发
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def validate(tool: Tool, args: Any) -> dict:
    """按 schema 查一遍。报错的话要让模型看得懂、改得动（「缺少 x」比一个异常栈有用）。"""
    if not isinstance(args, dict):
        raise ValueError("参数必须是一个 JSON 对象")
    props = tool.schema.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise ValueError(f"不认识的参数：{', '.join(sorted(unknown))}；可用的是 {', '.join(props) or '（无）'}")
    for k in tool.schema.get("required", []):
        if args.get(k) in (None, "", []):
            raise ValueError(f"缺少必填参数：{k}")
    out = {}
    for k, v in args.items():
        if v is None:
            continue
        spec = props[k]
        t = spec.get("type")
        if t == "integer" and isinstance(v, str) and v.strip().lstrip("-").isdigit():
            v = int(v)                         # 模型常把数字写成字符串
        if t == "integer" and isinstance(v, float) and v.is_integer():
            v = int(v)
        if t and not isinstance(v, _TYPES[t]) or (t in ("integer", "number") and isinstance(v, bool)):
            raise ValueError(f"参数 {k} 应该是 {t}")
        if t == "array" and spec.get("items", {}).get("type") == "integer":
            try:
                v = [int(i) for i in v]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"参数 {k} 应该是整数列表") from exc
        if "enum" in spec and v not in spec["enum"]:
            raise ValueError(f"参数 {k} 只能是 {' / '.join(spec['enum'])}")
        out[k] = v
    return out


def _clip(text: str) -> str:
    if len(text) <= TOOL_MAX_CHARS:
        return text
    return text[:TOOL_MAX_CHARS] + f"\n…（结果太长，只给了前 {TOOL_MAX_CHARS} 字。缩小范围再查；没看到的不要当作看到了）"


def dispatch(name: str, args: Any) -> dict:
    """执行一个调用。永不抛异常 —— 出错也变成一段模型看得懂的结果，让它自己改。"""
    t0 = time.perf_counter()
    tool = BY_NAME.get(name)
    if not tool:
        content, ok = f"没有这个工具：{name}。可用的是：{', '.join(BY_NAME)}", False
    else:
        try:
            clean = validate(tool, args)
            content, ok = json.dumps(tool.handler(**clean), ensure_ascii=False, default=str), True
        except (KeyError, ValueError) as exc:
            content, ok = f"出错了：{str(exc).strip(chr(39) + chr(34))}", False
        except Exception as exc:              # noqa: BLE001 — 工具里的意外也要回给模型，不能把整轮打断
            content, ok = f"工具执行失败：{type(exc).__name__}: {exc}", False
    return {"ok": ok, "content": _clip(content), "ms": round((time.perf_counter() - t0) * 1000)}


def preview(name: str, args: Any) -> str:
    """审批卡上的那句话。预览只读，失败了也给一句能看懂的。"""
    tool = BY_NAME.get(name)
    if not tool:
        return f"调用 {name}"
    try:
        clean = validate(tool, args)
        if tool.preview:
            return tool.preview(**clean)
    except Exception as exc:                  # noqa: BLE001
        return f"{tool.label}（参数有问题，批了也会失败：{exc}）"
    return f"{tool.label}：{json.dumps(clean, ensure_ascii=False)}"
