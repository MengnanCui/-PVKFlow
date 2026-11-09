"""智能体能调的工具。

照 IFlow（lib/agent/types.ts）的规矩，每个工具带一个**风险等级**，这是整套设计里最要紧的字段：

  · read          只读。直接跑，不打断人。
  · write         会改你的数据，但改得回来（审核状态、新建样品集）。默认停下来等你点一下；
                  你可以勾「本次对话内不再询问这一类」。
  · irreversible  改了回不来（删除样品集）。**每一次都问，勾不了，记不住。**

判错的成本必须落在「多点一下」这一侧。

工具只碰平台自己的库（样品、关键结果、样品集），**不碰文件系统、不跑命令** ——
这是一个「查数据、帮你审结果」的助手，不是一个能在你电脑上动手的代理。
参数校验在这里做：模型给的参数不可信。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.storage import db

READ, WRITE, IRREVERSIBLE = "read", "write", "irreversible"
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
