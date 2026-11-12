"""模型能看见什么，全在这个文件里定。

单独成模块是为了能单独测。上下文装配出错的表现是「模型答得头头是道但全是错的」——
这种错在界面上看不出来，只能靠测试把它钉住。

三条硬规则：

1. **原始光谱矩阵永远不进 prompt。** 一个样品 200 帧 × 2000 波长 = 40 万个数，
   一个样品就把本地 27B 的窗口撑爆了。给模型的是**曲线摘要**：批处理算出来的
   每条随时间变化的曲线（膜厚、积分、斜率、同事模块的曲线），按时间等分成
   十几到三十段、每段取平均（P-44，2026-11 你要求加的）。
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

# ── 曲线摘要（P-44）
# 每条曲线按时间等分成 N 段、每段取平均。N 由总预算倒推：样品多就少分几段，
# 但不少于 12 段（再少就看不出形状）、不多于 30 段（再多模型也读不过来）。
# 3000 个数 ≈ 18 KB 文本 ≈ 本地 27B 的五六千 token —— 和逐样品标量表是同一个量级。
CURVE_BINS_MIN = 12
CURVE_BINS_MAX = 30
CURVE_NUMBER_BUDGET = 3000


def build(scope: dict | None, *, detail_max: int = DETAIL_MAX,
          overview_only: bool = False, run_id: str = "") -> dict:
    """按范围拼一份事实包。

    scope 形如 `{"mode": "selected"|"all", "filter": {...}, "label": "..."}`。
    `mode` 只影响措辞（告诉模型这是你手挑的还是筛出来的），真正决定看什么的是
    `filter` —— 两种模式走的是同一条查询路径。

    `overview_only=True` 时只给 L1 概览：不给逐样品明细，也不发「请先收窄」
    那句指令。术语 ⓘ 弹窗走这条 —— 问「LOW_CYCLES 是什么意思」的时候，
    一张 40 行的样品表帮不上忙，反而会把模型带去提议筛选式。

    `run_id` 指定要带哪一次对比的诊断。在对比页上点开的 ⓘ 会带上它 ——
    问的是眼前这一次，而「最近一次」未必是它。

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
    elif overview_only:
        pass
    elif needs_narrowing:
        facts["为什么没有逐个样品的明细"] = (
            f"命中 {n} 个样品，超过一次能逐条阅读的上限（{detail_max} 个）。"
            "上面只有汇总统计。要看具体样品，请先给一个收窄的筛选式。"
        )
    else:
        facts["样品明细"] = _samples(flt, detail_max)

    comp = _comparison(run_id)
    if comp:
        here = run_id and comp["运行号"] == run_id
        facts["你正在看的这次对比" if here else "最近一次对比"] = comp

    # 曲线摘要放在**最后**：事实包超长时 to_prompt 从尾部截断，
    # 宁可少几条曲线，也不能把标量表和对比诊断截掉。
    if facts.get("样品明细"):
        curves = _curve_summaries(flt, detail_max)
        if curves:
            facts["曲线摘要"] = curves

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
        f"  FROM key_result_current WHERE sample_id IN ({marks})"
        f"  ORDER BY sample_id, field_name",
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
def _comparison(run_id: str = "") -> dict | None:
    """带上一次批处理的诊断。

    「这批膜厚为什么大半是 DEGRADED」是个很自然的问题，而答案
    （窗口太窄 → bin 太大 → 条纹数不够）全在这几个数里。不带上的话
    模型只能猜。

    `run_id` 给了就取**那一次**。你站在对比页上点开 ⓘ 提问，问的显然是
    眼前这一次 —— 而「最近一次」未必是它（你可能正翻着上周那次的结果）。
    没给就退回最近一次，AI 抽屉那条路一直是这个行为。
    """
    from app import batch as batch_mod

    r = None
    if run_id:
        r = db.query_one(
            "SELECT analysis_run_id, params_json, started_at,"
            "       (SELECT COUNT(*) FROM analysis_run c"
            "         WHERE c.parent_run_id = p.analysis_run_id) AS n_children,"
            "       (SELECT COUNT(*) FROM analysis_run c"
            "         WHERE c.parent_run_id = p.analysis_run_id"
            "           AND c.status = 'failed') AS n_failed"
            "  FROM analysis_run p WHERE p.analysis_run_id = ?", (run_id,))
    gone = bool(run_id) and r is None             # 问的那次已经从对比历史里删掉了（C077）
    if r is None:
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
    if gone:
        out["说明"] = f"你问的那次对比（{run_id}）已经从对比历史里删掉了，下面是最近一次对比"
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


# ---------------------------------------------------------------- L3 曲线摘要（P-44）
def _curve_summaries(flt: dict, limit: int) -> dict | None:
    """每个样品的曲线，压成几十个点给模型看形状。

    数据来自**最近一次跑过这个样品的批处理**的长表 —— 和「当前值」
    （key_result_current，也是最后一次为准）同一个口径。没跑过批处理的样品没有曲线，
    单独列出来，不编。

    下采样用**分段取平均**，不抽点：抽点会漏掉夹在两点之间的平台和突变，
    取平均不会（和热力图降采样用面积平均是同一个道理）。
    膜厚另给每段的「可信比例」—— 判据只打标志、不改数值，所以平均里含不可信的帧，
    比例必须跟着给（和对比页「不同时刻的平均膜厚」同一个口径，P-24）。
    """
    import numpy as np

    from app import batch as batch_mod
    from app.storage import tabular

    rows = selection.page(flt, limit=limit, offset=0, order="name")["rows"]
    if not rows:
        return None

    # 每个样品最近一次跑过它的批处理（父运行）
    latest_parent: dict[str, str] = {}
    for r in rows:
        hit = db.query_one(
            "SELECT parent_run_id FROM analysis_run WHERE sample_id = ?"
            " AND parent_run_id IS NOT NULL AND status = 'ok'"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1", (r["sample_id"],))
        if hit:
            latest_parent[r["sample_id"]] = hit["parent_run_id"]

    frames: dict[str, Any] = {}          # 表路径 → DataFrame，一张表只读一次
    catalogs: dict[str, list[dict]] = {}

    def table_df(parent: str, name: str):
        for t in tabular.tables_for_run(parent):
            if t["name"] != name:
                continue
            path = config.WORKSPACE / t["path"]
            if t["path"] not in frames:
                frames[t["path"]] = tabular.read_frame(path) if path.is_file() else None
            return frames[t["path"]]
        return None

    per_sample: list[tuple[dict, list[tuple[str, str, np.ndarray, np.ndarray]]]] = []
    missing: list[str] = []
    for r in rows:
        label = f"{r['batch']}/{r['name']}" if r.get("batch") else r["name"]
        parent = latest_parent.get(r["sample_id"])
        if not parent:
            missing.append(label)
            continue
        if parent not in catalogs:
            catalogs[parent] = batch_mod.curve_catalog(parent)
        series = []
        for c in catalogs[parent]:
            if not c["plottable"] and c["column"] != "ot_ok":
                continue
            df = table_df(parent, c["table"])
            if df is None or c["column"] not in df.columns:
                continue
            g = df[df["sample_id"] == r["sample_id"]]
            if g.empty:
                continue
            name = c["column"] if c["column"] == "ot_ok" else c["y_label"] or c["label"]
            series.append((name, c["column"], g["t"].to_numpy(dtype=float),
                           g[c["column"]].to_numpy(dtype=float)))
        if series:
            per_sample.append(({"label": label, "parent": parent}, series))
        else:
            missing.append(label)

    if not per_sample:
        return {"说明": "范围里的样品都还没跑过批处理，没有曲线。要看曲线先跑一次批处理。"} \
            if missing else None

    n_arrays = max(1 + len(s) for _, s in per_sample)        # 时间轴 + 每条曲线
    bins = int(np.clip(CURVE_NUMBER_BUDGET // max(1, len(per_sample) * n_arrays),
                       CURVE_BINS_MIN, CURVE_BINS_MAX))

    out: dict[str, Any] = {
        "说明": (f"每个样品取最近一次跑过它的批处理。每条曲线按时间等分成 {bins} 段、"
                 "每段取平均（不是抽点，平台期和突变都还在）；空段写 —。"
                 "「OT 可信比例」是这一段里判为可信的帧占多少（0–1），"
                 "膜厚的平均里含不可信的帧，读膜厚时要和它对着看。"),
    }
    for info, series in per_sample:
        t_all = np.concatenate([t for _, _, t, _ in series])
        t0, t1 = float(np.nanmin(t_all)), float(np.nanmax(t_all))
        edges = np.linspace(t0, t1, bins + 1) if t1 > t0 else np.array([t0, t0 + 1e-9])
        item: dict[str, Any] = {
            "来自对比": info["parent"],
            "时间 (s)": _fmt_series((edges[:-1] + edges[1:]) / 2),
        }
        for name, column, t, y in series:
            key = "OT 可信比例" if column == "ot_ok" else name
            if key in item:                     # 两条曲线标签撞了，用列名区分
                key = f"{key}（{column}）"
            item[key] = _fmt_series(_bin_mean(t, y, edges))
        out[info["label"]] = item
    if missing:
        out["没有曲线的样品"] = missing
    return out


def _bin_mean(t, y, edges):
    """按时间分段取平均。只算有限值；一段里一个有限值都没有就是 None。"""
    import numpy as np

    idx = np.clip(np.digitize(t, edges[1:-1]), 0, len(edges) - 2)
    out = []
    for k in range(len(edges) - 1):
        v = y[(idx == k) & np.isfinite(y)]
        out.append(float(v.mean()) if v.size else None)
    return out


def _fmt_series(vals) -> str:
    """一条曲线写成一行：`7067 6900 6512 —`。

    写成 json 数组的话 to_prompt 的 indent=2 会把每个数单独放一行，
    同样的信息多占三四倍篇幅。4 位有效数字：膜厚到 nm、积分到千分之一，够看形状了。
    """
    return " ".join("—" if v is None else f"{v:.4g}" for v in vals)


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
