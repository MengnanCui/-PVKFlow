"""筛选式 —— 「选中一批样品」的表示方法。

**一次选择是一个筛选式，不是一串 sample_id。**

在上千个样品的量级上，ID 列表这个模型直接就是坏的：放不进 URL、
一周后看不出当初选了什么、新导入的样品永远进不来。改成筛选式之后，
可复现、可分享、能随新数据自动生长 —— 一个决定同时解决三件事。

筛选式长这样（每一项都是「且」，项内多值是「或」）：

    {
      "batch":   ["B20", "B21"],          # 批次
      "folder":  ["2026-08"],             # 文件夹（display_path 的路径段前缀）
      "method":  ["spectrum"],            # 测量方法
      "import":  ["bat_xxx"],             # 导入批次
      "name_prefix": "B20_S",             # 名称前缀
      "name_range":  {"prefix": "B20_S", "min": 1, "max": 12},   # 名称里的可枚举段
      "time":    {"from": "2026-07-29T00:00", "to": "2026-07-30T00:00"},  # 测量时间
      "field":   [{"name":"PCE","min":20,"max":null}],           # 关键结果区间
      "ids":     ["smp_a", "smp_b"],      # 显式钉住的样品（固定集用）
      "exclude": ["smp_c"],               # 手工排除
      "q":       "S1"                     # 名称模糊搜索
    }

所有键都可选。空筛选式 = 全部样品。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.storage import db

# 允许出现在筛选式里的键。多一个都不认 —— 免得前端或模型塞进来奇怪的东西。
ALLOWED_KEYS = {
    "batch", "folder", "method", "import", "name_prefix", "name_range",
    "time", "field", "ids", "exclude", "q", "has_matrix",
}

_SAFE_FIELD = re.compile(r"^[\w.\-/%()][\w .\-/%()²³·]*$")


class FilterError(ValueError):
    """筛选式本身有问题。要说清楚是哪一项，别只说「无效」。"""


# ------------------------------------------------------------------ 规范化
def normalize(raw: Any) -> dict:
    """把外面传进来的东西整成一个干净的筛选式。不认识的键直接拒绝。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FilterError(f"筛选式不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise FilterError("筛选式必须是一个对象")

    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        raise FilterError(
            f"不认识的筛选项：{', '.join(sorted(unknown))}。"
            f"可用的是：{', '.join(sorted(ALLOWED_KEYS))}")

    out: dict = {}
    for key in ("batch", "folder", "method", "import", "ids", "exclude"):
        if key in raw and raw[key]:
            values = raw[key] if isinstance(raw[key], list) else [raw[key]]
            cleaned = [str(v).strip() for v in values if str(v).strip()]
            if cleaned:
                out[key] = cleaned

    if raw.get("q"):
        out["q"] = str(raw["q"]).strip()
    if raw.get("name_prefix"):
        out["name_prefix"] = str(raw["name_prefix"])
    if raw.get("has_matrix") is not None:
        out["has_matrix"] = bool(raw["has_matrix"])

    nr = raw.get("name_range")
    if nr:
        if not isinstance(nr, dict):
            raise FilterError("name_range 必须是对象")
        lo, hi = nr.get("min"), nr.get("max")
        if lo is None and hi is None:
            raise FilterError("name_range 至少要有 min 或 max")
        out["name_range"] = {
            "prefix": str(nr.get("prefix", "")),
            "suffix": str(nr.get("suffix", "")),
            "min": None if lo is None else int(lo),
            "max": None if hi is None else int(hi),
        }

    tw = raw.get("time")
    if tw:
        if not isinstance(tw, dict):
            raise FilterError("time 必须是对象，形如 {from: ISO, to: ISO}")
        a = str(tw.get("from") or "").strip()
        b = str(tw.get("to") or "").strip()
        if not a and not b:
            raise FilterError("time 至少要有 from 或 to")
        if a and b and a > b:
            a, b = b, a          # 端点反了就换过来，不用为这个报错
        out["time"] = {"from": a, "to": b}

    fields = raw.get("field")
    if fields:
        if isinstance(fields, dict):
            fields = [fields]
        cleaned_fields = []
        for f in fields:
            name = str(f.get("name", "")).strip()
            if not name:
                raise FilterError("field 筛选缺少 name")
            if not _SAFE_FIELD.match(name):
                raise FilterError(f"字段名里有不允许的字符：{name!r}")
            lo, hi = f.get("min"), f.get("max")
            if lo is None and hi is None:
                raise FilterError(f"字段 {name} 的筛选既没有 min 也没有 max")
            cleaned_fields.append({
                "name": name,
                "min": None if lo is None else float(lo),
                "max": None if hi is None else float(hi),
            })
        out["field"] = cleaned_fields

    return out


def is_empty(flt: dict) -> bool:
    return not normalize(flt)


# ------------------------------------------------------------------ 编译成 SQL
@dataclass
class Compiled:
    where: str                       # 不带 WHERE 关键字
    params: list = field(default_factory=list)
    joins: str = ""

    @property
    def clause(self) -> str:
        return f"WHERE {self.where}" if self.where else ""


def compile_filter(flt: dict) -> Compiled:
    """筛选式 → SQL 片段。

    全部走参数占位符，字段名单独用白名单正则挡过 —— 筛选式可能来自模型，
    不能让它拼进 SQL 里。
    """
    f = normalize(flt)
    where: list[str] = []
    params: list = []

    if f.get("batch"):
        where.append(f"s.batch IN ({','.join('?' * len(f['batch']))})")
        params += f["batch"]

    if f.get("method"):
        where.append(
            "EXISTS (SELECT 1 FROM measurement m WHERE m.sample_id = s.sample_id"
            f" AND m.method IN ({','.join('?' * len(f['method']))}))")
        params += f["method"]

    if f.get("import"):
        where.append(
            "EXISTS (SELECT 1 FROM artifact a WHERE a.sample_id = s.sample_id"
            f" AND a.batch_id IN ({','.join('?' * len(f['import']))}))")
        params += f["import"]

    if f.get("folder"):
        # 文件夹 = display_path 的路径前缀。目录树本身就是一棵分面树。
        subs = []
        for folder in f["folder"]:
            subs.append("a.display_path LIKE ?")
            params.append(folder.rstrip("/") + "/%")
        where.append(
            "EXISTS (SELECT 1 FROM artifact a WHERE a.sample_id = s.sample_id"
            f" AND ({' OR '.join(subs)}))")

    if f.get("q"):
        where.append("(s.name LIKE ? OR s.batch LIKE ?)")
        params += [f"%{f['q']}%"] * 2

    if f.get("name_prefix"):
        where.append("s.name LIKE ?")
        params.append(f["name_prefix"] + "%")

    if f.get("name_range"):
        nr = f["name_range"]
        # 名字里的可枚举段，比如 B20_S1 … B20_S48 里的 1..48。
        # SQLite 没有正则，用「切掉前后缀再转数字」实现，等价且能用上索引前缀。
        expr = "s.name"
        if nr["prefix"]:
            expr = f"substr({expr}, {len(nr['prefix']) + 1})"
            where.append("s.name LIKE ?")
            params.append(nr["prefix"] + "%")
        if nr["suffix"]:
            expr = f"substr({expr}, 1, length({expr}) - {len(nr['suffix'])})"
            where.append("s.name LIKE ?")
            params.append("%" + nr["suffix"])
        # 只保留那一段确实是纯数字的行，避免 CAST 把 'S1a' 悄悄变成 1
        where.append(f"({expr}) GLOB '[0-9]*' AND ({expr}) NOT GLOB '*[^0-9]*'")
        if nr["min"] is not None:
            where.append(f"CAST({expr} AS INTEGER) >= ?")
            params.append(nr["min"])
        if nr["max"] is not None:
            where.append(f"CAST({expr} AS INTEGER) <= ?")
            params.append(nr["max"])

    if f.get("time"):
        # 测量时间存在 measurement.measured_at 上（导入时从文件夹名的时间戳解析，
        # 解析不出来退回文件修改时间）。ISO 8601 是字典序可比的，
        # 所以直接字符串比较就行，不用 datetime 函数 —— 索引也才用得上。
        tw = f["time"]
        parts: list[str] = []
        if tw.get("from"):
            parts.append("m.measured_at >= ?")
            params.append(tw["from"])
        if tw.get("to"):
            parts.append("m.measured_at <= ?")
            params.append(tw["to"])
        where.append(
            "EXISTS (SELECT 1 FROM measurement m WHERE m.sample_id = s.sample_id"
            "        AND m.measured_at IS NOT NULL AND " + " AND ".join(parts) + ")")

    for spec in f.get("field", []):
        cond = ["k.sample_id = s.sample_id", "k.field_name = ?", "k.value_num IS NOT NULL"]
        params.append(spec["name"])
        if spec["min"] is not None:
            cond.append("k.value_num >= ?")
            params.append(spec["min"])
        if spec["max"] is not None:
            cond.append("k.value_num <= ?")
            params.append(spec["max"])
        where.append(f"EXISTS (SELECT 1 FROM key_result k WHERE {' AND '.join(cond)})")

    if f.get("has_matrix"):
        where.append(
            "EXISTS (SELECT 1 FROM artifact a WHERE a.sample_id = s.sample_id"
            " AND a.is_matrix = 1 AND a.status='ok')")

    if f.get("ids"):
        where.append(f"s.sample_id IN ({','.join('?' * len(f['ids']))})")
        params += f["ids"]

    if f.get("exclude"):
        where.append(f"s.sample_id NOT IN ({','.join('?' * len(f['exclude']))})")
        params += f["exclude"]

    return Compiled(where=" AND ".join(where), params=params)


# ------------------------------------------------------------------ 查询
def count(flt: dict) -> int:
    c = compile_filter(flt)
    return db.scalar(f"SELECT COUNT(*) FROM sample s {c.clause}", tuple(c.params)) or 0


def sample_ids(flt: dict, limit: int | None = None) -> list[str]:
    """展开成 ID 列表。只在真的要执行时用 —— 平时传筛选式就够了。"""
    c = compile_filter(flt)
    sql = f"SELECT s.sample_id FROM sample s {c.clause} ORDER BY s.name"
    params = list(c.params)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [r["sample_id"] for r in db.query(sql, tuple(params))]


def page(flt: dict, limit: int = 100, offset: int = 0, order: str = "name") -> dict:
    """服务端分页。上千个样品不能一次全推给前端。"""
    c = compile_filter(flt)
    order_sql = {
        "name": "s.name",
        "batch": "s.batch, s.name",
        "created": "s.created_at DESC, s.name",
    }.get(order, "s.name")

    total = db.scalar(f"SELECT COUNT(*) FROM sample s {c.clause}", tuple(c.params)) or 0
    # 矩阵信息用一次 JOIN 取回，不要三个相关子查询各扫一遍
    rows = db.query(
        f"""SELECT s.sample_id, s.name, s.batch, s.created_at,
                   (SELECT COUNT(*) FROM artifact a
                     WHERE a.sample_id = s.sample_id AND a.kind='raw') AS n_files,
                   (SELECT COUNT(*) FROM key_result k
                     WHERE k.sample_id = s.sample_id) AS n_results,
                   mx.artifact_id AS matrix_id,
                   mx.filename    AS matrix_name,
                   mx.size        AS matrix_size,
                   mx.status      AS matrix_status
            FROM sample s
            LEFT JOIN (
                SELECT a.sample_id, a.artifact_id, a.filename, a.size, a.status,
                       ROW_NUMBER() OVER (PARTITION BY a.sample_id ORDER BY a.size DESC) AS rn
                FROM artifact a WHERE a.is_matrix = 1
            ) mx ON mx.sample_id = s.sample_id AND mx.rn = 1
            {c.clause}
            ORDER BY {order_sql} LIMIT ? OFFSET ?""",
        tuple(c.params) + (limit, offset),
    )
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


# ------------------------------------------------------------------ 分面
# 分面全部来自已有字段，不需要新数据。每个分面带实时计数：
# 面内多选是「或」，跨面是「且」。计数是在**去掉自己这一面之后**的筛选式下算的 ——
# 否则选了 B20 之后其他批次全变成 0，就没法改选了。
def facets(flt: dict, top: int = 40) -> dict:
    f = normalize(flt)

    def without(key: str) -> Compiled:
        return compile_filter({k: v for k, v in f.items() if k != key})

    def bucket(key: str, sql: str, params_extra: tuple = ()) -> list[dict]:
        c = without(key)
        rows = db.query(sql.format(clause=c.clause), tuple(c.params) + params_extra)
        return [{"value": r["value"], "count": r["n"],
                 "selected": str(r["value"]) in set(f.get(key, []))}
                for r in rows if r["value"] is not None]

    out: dict = {}

    out["batch"] = bucket("batch", """
        SELECT s.batch AS value, COUNT(*) AS n FROM sample s {clause}
        GROUP BY s.batch ORDER BY n DESC, s.batch LIMIT ?""", (top,))

    out["method"] = bucket("method", """
        SELECT m.method AS value, COUNT(DISTINCT s.sample_id) AS n
        FROM sample s JOIN measurement m ON m.sample_id = s.sample_id {clause}
        GROUP BY m.method ORDER BY n DESC LIMIT ?""", (top,))

    out["import"] = []
    c = without("import")
    for r in db.query(f"""
        SELECT b.batch_id AS value, b.source_hint, b.created_at,
               COUNT(DISTINCT s.sample_id) AS n
        FROM sample s JOIN artifact a ON a.sample_id = s.sample_id
             JOIN import_batch b ON b.batch_id = a.batch_id
        {c.clause} GROUP BY b.batch_id ORDER BY b.created_at DESC LIMIT ?""",
            tuple(c.params) + (top,)):
        out["import"].append({
            "value": r["value"], "count": r["n"],
            "label": (r["source_hint"] or r["value"])[-48:],
            "created_at": r["created_at"],
            "selected": r["value"] in set(f.get("import", [])),
        })

    out["time"] = _time_facet(without("time"), f.get("time"))
    out["folder"] = _folder_facet(without("folder"), set(f.get("folder", [])), top)
    out["name"] = _name_facet(without("name_range"))
    out["field"] = _field_facet(without("field"), f.get("field", []))
    out["total"] = count(f)
    return out


def _time_facet(c: Compiled, selected: dict | None) -> dict:
    """测量时间的真实范围。

    跟名称滑块一个道理：端点就是数据里的真实 min/max，
    范围不用去别处读 —— 范围本身就画在那儿。
    """
    where = c.clause or "WHERE 1=1"
    row = db.query_one(
        f"SELECT MIN(m.measured_at) AS lo, MAX(m.measured_at) AS hi,"
        f"       COUNT(DISTINCT s.sample_id) AS n"
        f" FROM sample s JOIN measurement m ON m.sample_id = s.sample_id"
        f" {where} AND m.measured_at IS NOT NULL",
        tuple(c.params)) or {}
    return {"min": row.get("lo"), "max": row.get("hi"),
            "count": row.get("n") or 0, "selected": selected}


def _folder_facet(c: Compiled, selected: set[str], top: int) -> list[dict]:
    """文件夹分面。目录树本身就是一棵分面树 —— display_path 切一刀就有了。

    有一种情况要挡掉：原位数据是一个子文件夹一次测量，于是文件夹和样品
    一一对应，40 个样品出 40 个各含 1 个的 chip —— 那不是筛选，那是把
    列表又抄了一遍。**每个值都只有 1 个样品时整个分面就没有意义**，直接不给。
    （想按文件夹名找某一个，上面的搜索框就是干这个的。）
    """
    rows = db.query(f"""
        SELECT a.display_path AS p, s.sample_id AS sid
        FROM sample s JOIN artifact a ON a.sample_id = s.sample_id
        {c.clause} {'AND' if c.where else 'WHERE'} a.display_path LIKE '%/%'""",
        tuple(c.params))

    per_head: dict[str, set] = {}
    for r in rows:
        per_head.setdefault(str(r["p"]).split("/")[0], set()).add(r["sid"])

    if not per_head or max(len(v) for v in per_head.values()) < 2:
        return []

    out = [{"value": k, "count": len(v), "selected": k in selected}
           for k, v in per_head.items()]
    out.sort(key=lambda d: (-d["count"], d["value"]))
    return out[:top]


def _field_facet(c: Compiled, selected: list[dict]) -> list[dict]:
    """有哪些关键字段可以按数值筛，各自的范围是多少。"""
    sel = {s["name"]: s for s in selected}
    rows = db.query(f"""
        SELECT k.field_name AS name, k.unit AS unit,
               COUNT(DISTINCT s.sample_id) AS n,
               MIN(k.value_num) AS lo, MAX(k.value_num) AS hi
        FROM sample s JOIN key_result k ON k.sample_id = s.sample_id
        {c.clause} {'AND' if c.where else 'WHERE'} k.value_num IS NOT NULL
        GROUP BY k.field_name, k.unit
        HAVING COUNT(DISTINCT s.sample_id) > 1
        ORDER BY n DESC LIMIT 30""", tuple(c.params))
    return [{"name": r["name"], "unit": r["unit"], "count": r["n"],
             "min": r["lo"], "max": r["hi"],
             "selected": sel.get(r["name"])} for r in rows]


# 名称模式最多看这么多个样品。上万个样品时不必全扫，模式早就稳定了。
NAME_SCAN_LIMIT = 5000


def _name_facet(c: Compiled) -> dict:
    """名称里的可枚举段 —— 直接给前端渲染成双端滑块。

    这一条是为了回答「不想先去别处读范围再回来输」：滑块的端点就是数据里的
    真实 min/max，范围本身画在那儿，不用去查。
    """
    from app.storage.naming import detect_enumerations

    rows = db.query(
        f"SELECT s.name FROM sample s {c.clause} ORDER BY s.name LIMIT ?",
        tuple(c.params) + (NAME_SCAN_LIMIT,))
    names = [r["name"] for r in rows]
    return {
        "scanned": len(names),
        "truncated": len(names) >= NAME_SCAN_LIMIT,
        "patterns": [e.as_dict() for e in detect_enumerations(names)],
    }


# ------------------------------------------------------------------ 选择即示例
# 手点两三个之后，算出它们的共同点，主动提议扩展。
# 一次点击就把手工挑选变成规则 —— 比让人写查询式友好得多，而且完全确定性。
def suggest_expansion(sample_ids: list[str], base: dict | None = None,
                      max_suggestions: int = 5) -> list[dict]:
    """给一组手选的样品，返回若干条「要不要扩展到…」的提议。

    每条提议都是一个**筛选式**，点了就直接变成当前选择 —— 不是一串新的 ID。
    """
    if len(sample_ids) < 2:
        return []

    base = normalize(base or {})
    rows = db.query(
        f"SELECT sample_id, name, batch FROM sample"
        f" WHERE sample_id IN ({','.join('?' * len(sample_ids))})",
        tuple(sample_ids))
    if len(rows) < 2:
        return []

    picked = set(sample_ids)
    out: list[dict] = []

    def offer(label: str, why: str, flt: dict) -> None:
        merged = {**base, **flt}
        total = count(merged)
        if total <= len(picked):            # 没扩展出新东西就别提
            return
        out.append({"label": label, "why": why, "filter": merged,
                    "count": total, "adds": total - len(picked)})

    # ── 共同批次 ──
    batches = {r["batch"] for r in rows if r["batch"]}
    if len(batches) == 1:
        b = batches.pop()
        offer(f"选中 {b} 批次全部", f"选的 {len(rows)} 个都在 {b} 批次", {"batch": [b]})

    # ── 共同文件夹 ──
    paths = db.query(
        f"SELECT DISTINCT a.sample_id, a.display_path FROM artifact a"
        f" WHERE a.sample_id IN ({','.join('?' * len(sample_ids))})"
        f"   AND a.display_path LIKE '%/%'", tuple(sample_ids))
    heads = {str(p["display_path"]).split("/")[0] for p in paths}
    if len(heads) == 1:
        h = heads.pop()
        offer(f"选中 {h}/ 下全部", f"选的都在 {h}/ 目录下", {"folder": [h]})

    # ── 名称里的可枚举段 ──
    from app.storage.naming import detect_enumerations

    names = [r["name"] for r in rows]
    for enum in detect_enumerations(names, min_members=2, limit=3):
        if enum.count < len(rows):
            continue
        # 先看整段范围有多少个，再决定提议扩到哪
        probe = {**base, "name_range": {"prefix": enum.prefix, "suffix": enum.suffix,
                                        "min": None, "max": None}}
        full = db.query(
            f"SELECT s.name FROM sample s WHERE s.name LIKE ? ORDER BY s.name",
            (enum.prefix + "%" + enum.suffix,))
        all_nums = []
        for r in full:
            mid = r["name"][len(enum.prefix):len(r["name"]) - len(enum.suffix) or None]
            if mid.isdigit():
                all_nums.append(int(mid))
        if len(all_nums) <= len(rows):
            continue
        offer(f"扩展到 {enum.prefix}{min(all_nums)}–{max(all_nums)}{enum.suffix}",
              f"选的是 {enum.prefix}{enum.min}–{enum.max}{enum.suffix}，共 {len(all_nums)} 个同名模式",
              {"name_range": {"prefix": enum.prefix, "suffix": enum.suffix,
                              "min": min(all_nums), "max": max(all_nums)}})

    # ── 共同测量方法 ──
    methods = db.query(
        f"SELECT m.method, COUNT(DISTINCT m.sample_id) AS n FROM measurement m"
        f" WHERE m.sample_id IN ({','.join('?' * len(sample_ids))})"
        f" GROUP BY m.method", tuple(sample_ids))
    for m in methods:
        if m["n"] == len(rows) and m["method"]:
            offer(f"选中所有 {m['method']} 测量的样品",
                  f"选的 {len(rows)} 个都做过 {m['method']}", {"method": [m["method"]]})

    out.sort(key=lambda d: d["adds"])       # 扩得少的排前面，比较不吓人
    return out[:max_suggestions]
