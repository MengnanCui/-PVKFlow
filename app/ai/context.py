"""模型能看见什么，全在这个文件里定。

单独成模块是为了能单独测。上下文装配出错的表现是「模型答得头头是道但全是错的」——
这种错在界面上看不出来，只能靠测试把它钉住。

三条硬规则：

1. **原始光谱矩阵永远不进 prompt。** 一个样品 200 帧 × 2000 波长 = 40 万个数，
   一个样品就把本地 27B 的窗口撑爆了。模型要看曲线就去看标量，或者你自己看图。
2. **超过 `AI_DETAIL_MAX` 个样品时不给逐条明细**，只给概览，并明说
   「请先给一个收窄的筛选式」。宁可多问一轮，也不要模型对着 200 个样品的
   截断列表编一个「综合来看」。
3. **范围是筛选式，不是 ID 列表。** 传进来的 scope 带 filter，这里现算。
"""
from __future__ import annotations

import json
from typing import Any

from app import config
from app.storage import db, selection

# 逐样品明细的条数上限。超过就先让模型收窄。
# 40 行 × 约 40 token ≈ 1.6k token，本地 27B 也吃得下；再多就开始挤掉对话历史了。
DETAIL_MAX = getattr(config, "AI_DETAIL_MAX", 40)

# 送进 prompt 的事实块的字节上限。这是最后一道闸 —— 上面两条规则都失效时，
# 至少不会把一个几 MB 的 json 甩给模型。
FACTS_BYTE_LIMIT = 60_000


def build(scope: dict | None, *, detail_max: int = DETAIL_MAX) -> dict:
    """按范围拼一份事实包。

    scope 形如 `{"mode": "selected"|"all", "filter": {...}, "label": "..."}`。
    `mode` 只影响措辞（告诉模型这是你手挑的还是筛出来的），真正决定看什么的是
    `filter` —— 两种模式走的是同一条查询路径。

    返回 `{"facts": {...}, "n_samples": int, "needs_narrowing": bool}`。
    """
    scope = scope or {}
    flt = scope.get("filter") or {}
    mode = scope.get("mode") or ("selected" if flt.get("ids") else "all")

    n = selection.count(flt)
    needs_narrowing = n > detail_max

    facts: dict[str, Any] = {
        "范围": {
            "来源": "你在界面上手工勾选的" if mode == "selected" else "当前筛选式命中的",
            "样品数": n,
            "筛选式": flt or "（无筛选，全库）",
        },
        "概览": _overview(flt),
    }

    if n == 0:
        facts["提示"] = "这个范围里一个样品都没有。请提醒用户先导入数据或放宽筛选。"
    elif needs_narrowing:
        facts["为什么没有逐个样品的明细"] = (
            f"命中 {n} 个样品，超过一次能逐条阅读的上限（{detail_max} 个）。"
            "上面只有汇总统计。要看具体样品，请先给一个收窄的筛选式。"
        )
    else:
        facts["样品明细"] = _samples(flt, detail_max)

    last = _last_comparison()
    if last:
        facts["最近一次对比"] = last

    return {"facts": facts, "n_samples": n, "needs_narrowing": needs_narrowing,
            "mode": mode, "filter": flt}


# ---------------------------------------------------------------- L1 概览
def _overview(flt: dict) -> dict:
    """汇总统计。复用 selection.facets —— 那套分面计数本来就是干这个的，
    不为模型另写一份聚合（两份聚合迟早会对不上，那时候没人知道该信哪个）。"""
    f = selection.facets(flt, top=12)

    out: dict[str, Any] = {"命中样品数": f.get("total", 0)}

    devices = [(x["value"], x["count"]) for x in f.get("batch") or []]
    if devices:
        out["样品号分布"] = {k: v for k, v in devices}

    t = f.get("time") or {}
    if t.get("min"):
        out["测量时间范围"] = {"最早": t["min"], "最晚": t["max"],
                              "有时间戳的样品数": t.get("count", 0)}

    fields = []
    for fd in f.get("field") or []:
        fields.append({
            "字段": fd["name"], "单位": fd.get("unit") or "",
            "样品数": fd.get("count"),
            "最小": _round(fd.get("min")), "最大": _round(fd.get("max")),
        })
    if fields:
        out["关键结果字段"] = fields

    folders = [(x["value"], x["count"]) for x in f.get("folder") or []]
    if folders:
        out["文件夹分布"] = {k: v for k, v in folders}

    return out


# ---------------------------------------------------------------- L2 明细
def _samples(flt: dict, limit: int) -> list[dict]:
    """一个样品一行标量。曲线不进来，只进曲线的**结论**。"""
    p = selection.page(flt, limit=limit, offset=0, order="name")
    rows = p["rows"]
    if not rows:
        return []

    ids = [r["sample_id"] for r in rows]
    scalars = _scalars_for(ids)

    out = []
    for r in rows:
        item = {
            "样品": r["name"],
            "样品号": r.get("batch") or "",
            "测量时间": r.get("measured_at") or "",
            "有光谱矩阵": bool(r.get("matrix_id")),
        }
        vals = scalars.get(r["sample_id"])
        if vals:
            item["关键结果"] = vals
        out.append(item)
    return out


def _scalars_for(sample_ids: list[str]) -> dict[str, dict]:
    """一次查完所有样品的关键结果。

    按 sample_id 逐个查会变成 N+1 —— 40 个样品 40 次查询，本身不慢，
    但这个函数以后会被更大的 N 调用，先按能扩展的写法写。
    """
    if not sample_ids:
        return {}
    marks = ",".join("?" * len(sample_ids))
    rows = db.query(
        f"SELECT sample_id, field_name, value_num, value_text, unit"
        f"  FROM key_result WHERE sample_id IN ({marks})"
        f"   AND quality != 'reject' ORDER BY sample_id, field_name",
        tuple(sample_ids))

    out: dict[str, dict] = {}
    for r in rows:
        v = r["value_num"] if r["value_num"] is not None else r["value_text"]
        if v is None:
            continue
        key = f"{r['field_name']}({r['unit']})" if r["unit"] else r["field_name"]
        out.setdefault(r["sample_id"], {})[key] = _round(v)
    return out


# ---------------------------------------------------------------- 最近一次对比
def _last_comparison() -> dict | None:
    """带上最近一次批处理的诊断。

    「这批膜厚为什么大半是 DEGRADED」是个很自然的问题，而答案
    （窗口太窄 → bin 太大 → 条纹数不够）全在这几个数里。不带上的话
    模型只能猜。
    """
    from app import batch as batch_mod

    runs = batch_mod.recent_batches(limit=1)
    if not runs:
        return None
    r = runs[0]
    try:
        params = json.loads(r.get("params_json") or "{}")
    except json.JSONDecodeError:
        params = {}
    recipe = params.get("recipe") or {}

    out = {
        "名称": params.get("title") or "未命名对比",
        "运行号": r["analysis_run_id"],
        "样品数": r.get("n_children", 0),
        "失败数": r.get("n_failed", 0),
        "时间": r.get("started_at"),
    }
    if recipe.get("band_min"):
        out["膜厚窗口(nm)"] = [recipe["band_min"], recipe["band_max"]]

    # 这次对比算出来的标量的分布。批处理写进 key_result 的就是这些
    # （ot_floor / fringe_bin / integral_* / slope_abs_max / n_time / n_lambda），
    # 全是数值 —— 所以这里聚合 value_num，不去找并不存在的状态文本字段。
    stats = db.query(
        "SELECT field_name, unit, COUNT(*) AS n,"
        "       MIN(value_num) AS lo, MAX(value_num) AS hi, AVG(value_num) AS avg"
        "  FROM key_result"
        "  WHERE analysis_run_id IN (SELECT analysis_run_id FROM analysis_run"
        "                             WHERE parent_run_id = ?)"
        "    AND value_num IS NOT NULL GROUP BY field_name, unit ORDER BY field_name",
        (r["analysis_run_id"],))
    if stats:
        out["这次对比的标量分布"] = [
            {"字段": s["field_name"], "单位": s["unit"] or "", "样品数": s["n"],
             "最小": _round(s["lo"]), "最大": _round(s["hi"]), "均值": _round(s["avg"])}
            for s in stats]
    return out


# ---------------------------------------------------------------- 序列化
def to_prompt(facts: dict, *, byte_limit: int = FACTS_BYTE_LIMIT) -> str:
    """事实包 → 喂给模型的文本。

    超限时**截断并说出来**。静默截断的话模型会对着半个列表做全局判断，
    而且没人知道它看漏了 —— 跟批处理表格截断要写「共 N 帧」是同一条原则。
    """
    text = json.dumps(facts, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) <= byte_limit:
        return text
    cut = text.encode("utf-8")[:byte_limit].decode("utf-8", "ignore")
    return cut + "\n…（事实包超长，已截断。上面看到的不是全部，"\
                 "请先收窄范围再下结论。）"


def _round(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 4)
    return v
