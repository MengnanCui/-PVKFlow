"""构效关系的数据：把关键结果「当前值」透视成每个样品一行。

不另建数据模型 —— key_result 是长表，按 field_name 取两列就是 X / Y（DATA_MODEL.md
「key_result 为什么是长表」）。读的是 key_result_current：同一个样品跑过三次批处理，
散点图上也只有一个点。

样品范围用的是和数据处理页同一个筛选式（selection.compile_filter）：那一页筛出什么，
这里就分析什么。
"""
from __future__ import annotations

import math

from app.analysis import stats
from app.storage import db, results, selection

# 分组的依据。第一版只有「批次」（仪器文件夹名里的样品号，数据处理页那个分面）。
GROUP_BY = {"batch": "s.batch"}
NO_GROUP = "（无批次）"


def _field(name: str) -> dict:
    """字段的显示信息（中文名、单位）。不存在或不是数值字段就报错 —— 拿一个没有的字段算出
    「0 个样品」，看起来像是数据问题，其实是选错了。"""
    for f in results.list_fields():
        if f["field_name"] == name:
            if not f["n_numeric"]:
                raise ValueError(f"字段「{name}」没有数值，没法做相关或箱线图")
            return {k: f[k] for k in ("field_name", "label", "unit", "units", "mixed_units", "n_numeric")}
    raise ValueError(f"没有这个字段：{name}")


def _num(v) -> float | None:
    return float(v) if v is not None and math.isfinite(float(v)) else None


def pairs(x: str, y: str, flt: dict | None = None) -> dict:
    """范围内每个样品的 (X, Y) 当前值 + 相关统计。

    只有一个字段有值的样品不进散点，但要数出来：「40 个样品里只有 12 个两个都有」
    本身就是该知道的事。
    """
    fx, fy = _field(x), _field(y)
    c = selection.compile_filter(flt or {})
    rows = db.query(
        f"""SELECT s.sample_id, s.name, s.batch, kx.value_num AS x, ky.value_num AS y
            FROM sample s
            LEFT JOIN key_result_current kx ON kx.sample_id = s.sample_id AND kx.field_name = ?
            LEFT JOIN key_result_current ky ON ky.sample_id = s.sample_id AND ky.field_name = ?
            {c.clause}
            ORDER BY s.batch, s.name""",
        (x, y, *c.params))

    points, only_x, only_y = [], 0, 0
    for r in rows:
        vx, vy = _num(r["x"]), _num(r["y"])
        if vx is not None and vy is not None:
            points.append({"sample_id": r["sample_id"], "name": r["name"],
                           "batch": r["batch"] or "", "x": vx, "y": vy})
        elif vx is not None:
            only_x += 1
        elif vy is not None:
            only_y += 1
    corr = stats.correlation([p["x"] for p in points], [p["y"] for p in points])
    return {
        "x": fx, "y": fy, "points": points, "stats": corr,
        "n_scope": len(rows), "n_only_x": only_x, "n_only_y": only_y,
        "n_neither": len(rows) - len(points) - only_x - only_y,
    }


def groups(y: str, by: str = "batch", flt: dict | None = None) -> dict:
    """按批次分组的箱线图统计。离群点带上是哪个样品 —— 箱线图上一个孤点，你下一步就是去看它。"""
    if by not in GROUP_BY:
        raise ValueError(f"分组只能按：{'、'.join(GROUP_BY)}")
    fy = _field(y)
    c = selection.compile_filter(flt or {})
    rows = db.query(
        f"""SELECT s.sample_id, s.name, {GROUP_BY[by]} AS g, ky.value_num AS y
            FROM sample s
            JOIN key_result_current ky ON ky.sample_id = s.sample_id AND ky.field_name = ?
            {c.clause}
            ORDER BY g, s.name""",
        (y, *c.params))

    by_group: dict[str, list[dict]] = {}
    for r in rows:
        v = _num(r["y"])
        if v is not None:
            by_group.setdefault(r["g"] or NO_GROUP, []).append(r | {"y": v})
    out = []
    for name in sorted(by_group):
        members = by_group[name]
        b = stats.box_stats([m["y"] for m in members])
        b["outliers"] = [{"sample_id": members[i]["sample_id"], "name": members[i]["name"],
                          "y": members[i]["y"]} for i in b["outliers"]]
        out.append({"name": name, **b})
    return {"y": fy, "by": by, "groups": out, "n": sum(g["n"] for g in out)}
