"""批处理结果页（对比页）要的数据：曲线、时刻切片、轴范围、导出脚本。**都是只读查询**，不改任何东西。

原来写在路由文件 app/api/batch.py 里（C074 挪出来）：路由只管参数和把错误翻成 HTTP。
这里抛 NotFound / Invalid（app/errors.py），带上 kind 让前端分得清是哪种错。
"""
from __future__ import annotations

import numpy as np

from app import batch as batch_mod
from app import config
from app.errors import Invalid, NotFound
from app.parsers import matrix
from app.plotting import script_export
from app.storage import artifacts, db, tabular
from app.util.csvsafe import guard_formula

# 列名的形状。具体有哪些列不在这里写死 —— 由这次批处理的长表决定
# （见 batch_mod.curve_catalog）。原来写死成 integral|slope|ot，同事模块的曲线取不出来。
COLUMN_RE = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


def detail(parent_run_id: str) -> dict:
    """一次批处理的详情 + 对比页要的参数面板和曲线目录。"""
    d = batch_mod.batch_detail(parent_run_id)
    # 对比页要照着模块声明画参数控件，每个控件填什么值由后端算 ——
    # 「默认值 ← 老配方映射 ← 显式指定」这条规则只有 _params_for 一份实现，
    # 前端再抄一遍迟早对不上。
    try:
        recipe = batch_mod.Recipe.from_dict((d.get("run") or {}).get("params", {}).get("recipe"))
        d["module_params"] = batch_mod.effective_params(recipe)
    except (ValueError, KeyError, AttributeError):
        # 老配方读不出来不该把整页拖垮 —— 参数面板退回模块自己的默认值
        d["module_params"] = {}
    # 这次长表里实际有哪些曲线 —— 对比页照它画，同事模块的曲线也在里面
    d["curves"] = batch_mod.curve_catalog(parent_run_id)
    return d


def axes(parent_run_id: str) -> dict:
    """这批数据的波长轴和时间轴范围。

    模块的控件可以声明 `range_from="lambda"` —— 意思是「上下限跟着这份数据的
    波长范围走」。样品页上那份范围来自当前打开的矩阵；对比页没有「当前矩阵」，
    所以在这里给：拿这批里**一个**样品的元信息代表整批（同一台光谱仪，波长轴一样）。

    读不到不是故障 —— 界面会退回控件自己声明的 min/max，所以这里**不抛错**，返回 available=false。
    """
    d = batch_mod.batch_detail(parent_run_id)
    sample_ids = [c["sample_id"] for c in d["children"] if c.get("sample_id")]
    if not sample_ids:
        return {"available": False, "reason": "这次批处理没有样品"}

    row = db.query_one(
        "SELECT artifact_id FROM artifact WHERE sample_id=? AND is_matrix=1"
        " ORDER BY size DESC LIMIT 1", (sample_ids[0],))
    if not row:
        return {"available": False, "reason": "找不到这批样品的光谱矩阵"}

    try:
        sm = matrix.load_cached(artifacts.local_path(row["artifact_id"]))
    except Exception as exc:                      # noqa: BLE001 —— 读不到就降级
        return {"available": False, "reason": f"读不出光谱矩阵：{exc}"}

    return {
        "available": True,
        "from_sample": sample_ids[0],
        "lambda": [float(sm.lam[0]), float(sm.lam[-1])],
        "time": [float(sm.t[0]), float(sm.t[-1])],
    }


def load_curves(parent_run_id: str, column: str):
    """读出这次批处理的长表。curves、slices 和 export 走的是同一份数据。

    膜厚在**另一张**表里（batch_thickness）：它的时间轴是全部帧，
    batch_curves 那张可能被 max_time_points 抽稀过。哪一列在哪张表，问 curve_catalog。
    """
    detail_ = batch_mod.batch_detail(parent_run_id)
    catalog = batch_mod.curve_catalog(parent_run_id)
    entry = next((c for c in catalog if c["column"] == column), None)
    if entry is not None:
        want = entry["table"]
    elif column in batch_mod.BUILTIN_CURVES:
        # 平台那三条缺席，多半是老对比（跑在加膜厚之前），下面会说清楚「重跑一次就有」
        want = "batch_thickness" if column == "ot" else "batch_curves"
    else:
        have = "、".join(c["column"] for c in catalog) or "（一列都没有）"
        raise Invalid(f"这次批处理没有「{column}」这一列。有的是：{have}", "bad_column")
    tables = [t for t in detail_["tables"] if t["name"] == want]
    if not tables and want == "batch_thickness":
        raise NotFound("这次对比没有膜厚数据。多半是它跑在加膜厚之前 —— 用上面的参数重跑一次就有了。",
                       "no_thickness")
    if not tables:
        raise NotFound("这次批处理没有留下曲线表", "no_curves")

    path = config.WORKSPACE / tables[0]["path"]
    if not path.is_file():
        raise NotFound(f"曲线表文件缺失：{tables[0]['path']}", "missing_table")
    df = tabular.read_frame(path)
    if column not in df.columns:
        raise Invalid(f"这张表里没有 {column} 列", "bad_column")
    return detail_, tables[0], df


def curve_series(parent_run_id: str, column: str, max_series: int) -> dict:
    """批处理的曲线，按样品分组返回。

    上千条曲线叠一张图是噪声不是图 —— 超过阈值时前端会降级成分位数带，
    所以这里控制一下返回的序列上限，并如实告知截断了多少。
    """
    _detail, table, df = load_curves(parent_run_id, column)

    groups = list(df.groupby("sample_id", sort=False))
    truncated = max(0, len(groups) - max_series)
    series = []
    for sid, g in groups[:max_series]:
        y = g[column].tolist()
        series.append({
            "sample_id": sid,
            # 图例里只写 S1 的话，24 个批次的 S1 在图上分不出是哪一个
            "label": (f"{g['batch'].iloc[0]}/{g['sample_name'].iloc[0]}"
                      if g["batch"].iloc[0] else str(g["sample_name"].iloc[0])),
            "group": str(g["batch"].iloc[0] or ""),
            "x": [round(float(v), 4) for v in g["t"]],
            "y": [None if v is None or v != v else round(float(v), 6) for v in y],
        })
    return {"column": column, "series": series,
            "n_series": len(groups), "returned": len(series), "truncated": truncated,
            "table_id": table["table_id"]}


def slices(parent_run_id: str, windows: str) -> dict:
    """不同样品在若干个时间窗内的平均膜厚。

    **这是查询，不是配方。** 膜厚曲线在批处理时就整条算好存下了，
    这里只是按时间窗切一刀求平均 —— 所以加一个「再看看 15 秒」是即时的，
    不用重跑。只有改膜厚的波长窗口才要重跑（OT 本来就依赖那个窗口）。

    平均口径：**窗口内全部帧都参与**，另外给一个 ok_ratio 说明其中多少帧可信。
    这是用户定的口径 —— 不可信的帧会把均值拉偏，所以那个比例必须一起显示，
    不能只给一个漂亮的数。

    窗口里一帧都没有（比如样品只测到 21 s，你问 28 s）时 mean 返回 null
    并说明原因。**绝不拿最近的一帧顶替** —— 那会让「这批数据根本没测到 28 s」
    这个事实消失，而它恰恰是你需要知道的。
    """
    wins = parse_windows(windows)
    _detail, table, df = load_curves(parent_run_id, "ot")

    rows = []
    for sid, g in df.groupby("sample_id", sort=False):
        t = g["t"].to_numpy()
        ot = g["ot"].to_numpy()
        # 「可信」是模块算出来的一列 0/1（膜厚模块的 batch_extra["ot_ok"]），
        # 不是这里重新判的 —— 判据只有一份，在 fringe_ot 里。
        # 老的批处理存的是 status 字符串列，还读得出来。
        if "ot_ok" in g.columns:
            ok_mask = g["ot_ok"].to_numpy() > 0.5
        elif "status" in g.columns:
            ok_mask = g["status"].astype(str).to_numpy() == "OK"
        else:
            ok_mask = np.zeros(len(ot), dtype=bool)
        t_lo, t_hi = (float(t.min()), float(t.max())) if len(t) else (0.0, 0.0)

        values = []
        for lo, hi in wins:
            sel = (t >= lo) & (t <= hi)
            vals = ot[sel]
            finite = vals[np.isfinite(vals)]
            if not finite.size:
                values.append({
                    "mean": None, "n_frames": 0, "n_ok": 0, "ok_ratio": None,
                    "note": (f"超出该样品的时间范围（{t_lo:g}–{t_hi:g} s）"
                             if lo > t_hi or hi < t_lo else "这个窗口里没有有效帧"),
                })
                continue
            n_ok = int(ok_mask[sel][np.isfinite(vals)].sum())
            # 窗口里没有数的帧（输入缺失，见膜厚模块的 NO_INPUT）不参与平均 ——
            # 「全部帧参与」说的是有数的帧都算，不是把没有的数编出来。少了几帧要说出来。
            n_missing = int(vals.size - finite.size)
            values.append({
                "mean": round(float(finite.mean()), 2),
                "std": round(float(finite.std()), 2),
                "n_frames": int(finite.size),
                "n_ok": n_ok,
                "ok_ratio": round(n_ok / finite.size, 3),
                "n_missing": n_missing,
                "note": (f"窗口里有 {n_missing} 帧没有数（输入缺失），没参与平均"
                         if n_missing else ""),
            })

        rows.append({
            "sample_id": sid,
            "label": (f"{g['batch'].iloc[0]}/{g['sample_name'].iloc[0]}"
                      if g["batch"].iloc[0] else str(g["sample_name"].iloc[0])),
            "group": str(g["batch"].iloc[0] or ""),
            "t_min": round(t_lo, 3), "t_max": round(t_hi, 3),
            "values": values,
        })

    return {
        "windows": [{"from": lo, "to": hi} for lo, hi in wins],
        "rows": rows,
        "n_samples": len(rows),
        "unit": "nm",
        "table_id": table["table_id"],
    }


def parse_windows(raw: str) -> list[tuple[float, float]]:
    """`0:1,27.5:28.5` → [(0,1), (27.5,28.5)]。看不懂就说清楚哪一段看不懂。"""
    out: list[tuple[float, float]] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        part = chunk.split(":")
        if len(part) != 2:
            raise Invalid(f"时间窗要写成「起:止」，看不懂这个：{chunk}", "bad_window")
        try:
            lo, hi = float(part[0]), float(part[1])
        except ValueError:
            raise Invalid(f"时间窗里不是数字：{chunk}", "bad_window") from None
        if hi < lo:
            lo, hi = hi, lo
        out.append((lo, hi))
    if not out:
        raise Invalid("至少要给一个时间窗", "bad_window")
    if len(out) > 12:
        raise Invalid(f"一次最多 12 个时间窗，给了 {len(out)} 个", "too_many")
    return out


def y_labels(parent_run_id: str, column: str) -> tuple[str, str]:
    """平台那三条用写好的中英文标签；同事模块的曲线用它声明里的标签（英文没有就同一个）。"""
    if column in batch_mod.BUILTIN_Y_LABELS:
        return batch_mod.BUILTIN_Y_LABELS[column]
    entry = next((c for c in batch_mod.curve_catalog(parent_run_id)
                  if c["column"] == column), None)
    label = (entry or {}).get("y_label") or column
    return label, label


def export_bundle(parent_run_id: str, column: str, mode: str, group_by: str,
                  max_series: int) -> tuple[bytes, str]:
    """打包 plot.py + data.csv + README.md，返回 (zip 内容, 文件名)。"""
    detail_, _table, df = load_curves(parent_run_id, column)

    # 一条曲线 = 一个 sample_id。样品的身份是 (名字, 批次)，S1 在每个批次
    # 里都有一个 —— 按名字截断或分组会把不同批次的同名样品合成一条。
    sids = list(dict.fromkeys(df["sample_id"]))
    if len(sids) > max_series:
        sids = sids[:max_series]
        df = df[df["sample_id"].isin(set(sids))]

    df = df.copy()
    df["label"] = [f"{b}/{n}" if b else str(n)
                   for b, n in zip(df["batch"].fillna(""), df["sample_name"])]
    # 名字来自文件夹名：以 = + - @ 开头的，Excel 打开 data.csv 会当公式执行。前面加 '，
    # 导出的 plot.py 读回时去掉（guard_formula）
    for c in script_export.TEXT_COLUMNS:
        df[c] = [guard_formula(v) for v in df[c]]
    cols = ["sample_id", "sample_name", "batch", "label", "t", column]
    csv_bytes = df[cols].to_csv(index=False).encode("utf-8-sig")

    zh_label, en_label = y_labels(parent_run_id, column)
    blob = script_export.build_zip(
        csv_bytes=csv_bytes,
        column=column,
        y_label=zh_label,
        y_label_en=en_label,
        mode=mode,
        group_by=group_by,
        n_series=len(sids),
        n_rows=len(df),
        recipe=(detail_["run"].get("params") or {}).get("recipe") or {},
        run_id=parent_run_id,
        title=f"{zh_label} · {len(sids)} 个样品",
        title_en=f"{en_label} · {len(sids)} samples",
    )
    return blob, f"hte-{column}-{parent_run_id[:8]}.zip"


def export_preview(parent_run_id: str, column: str, mode: str, group_by: str) -> dict:
    """先读脚本，再决定要不要下载。"""
    _detail, _table, df = load_curves(parent_run_id, column)
    zh_label, en_label = y_labels(parent_run_id, column)
    n_series = df["sample_id"].nunique()
    script = script_export.build_script(
        column=column, y_label=zh_label, y_label_en=en_label, mode=mode,
        group_by=group_by, n_series=int(n_series),
        title=f"{zh_label} · {n_series} 个样品",
        title_en=f"{en_label} · {n_series} samples")
    return {"script": script, "n_series": int(n_series), "n_rows": len(df),
            "columns": ["sample_id", "sample_name", "batch", "label", "t", column]}
