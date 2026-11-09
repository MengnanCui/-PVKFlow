"""一次 QFLS 处理：扫描 → 逐帧（校验、指标、出图）→ 全局（组内标准化、离群、分布、排名、全局图）→ 落盘。

**输出全在独立目录 `workspace/qfls/<run_id>/`，原始数据只读、一个字节都不改**（打开文件只用 "rb"）：

    run.json        这次处理的参数、状态、计数
    scan.json       扫描报告：认出的实验 / 批次、跳过的目录和文件（带原因）、修过的乱码名、历史产物数
    points.json     每个样点一行：身份（实验 / 批次 / slot / point / 物镜 / 光强 / 时间）+ 全部指标 + 分 + 等级 + 异常 + 组内 z
    points.csv      同上，Excel 能直接开（UTF-8 带 BOM、文字格子防公式）
    summary.json    全局：分组统计、箱线图数据、排名、等级分布、编号对照
    README.md       这个目录里每个文件、每个字段是什么，以及不能怎么用（不同物镜不可比……）
    global/         boxplots.png / ranking.png / grades.png
    points/<key>/   metrics.json + heatmap / hist / profile / anomaly / variogram / summary / heatmap_group .png
"""
from __future__ import annotations

import csv
import json
import math
import secrets
import shutil
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from app import config, tasks
from app.qfls import frame as F
from app.qfls import metrics as M
from app.qfls import render as R
from app.qfls import scan as S

RUN_FILES = ("run.json", "scan.json", "points.json", "points.csv", "summary.json", "README.md")


def runs_dir() -> Path:
    return config.WORKSPACE / "qfls"


def demo_dir() -> Path:
    return config.WORKSPACE / "qfls_demo"


def _dump(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=_jsonable), encoding="utf-8")
    tmp.replace(path)


def _jsonable(o):
    if isinstance(o, (np.floating,)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def new_run_id() -> str:
    """qr_日期_时间_6 位随机 hex：按时间排得出先后，同一秒提交两次也撞不上（原来只有 3 位，C069）。"""
    return "qr_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)


def merge_params(overrides: dict | None) -> dict:
    """平台默认阈值 + 这一次的覆盖。键要认识、类型和形状要对上（数字 / 列表长度 / 嵌套字典）；
    嵌套的（score、grades）做**深合并** —— 只改一项扣分不会把整张扣分表换掉。不合法就 ValueError（接口回 400）。

    原来只核对键名：valid_range_ev=5 这种会让每一帧都「处理出错」、却报一个 ok（体检发现，C069）。
    """
    P = json.loads(json.dumps(M.PARAMS))

    def merge(base, new, path):
        if isinstance(base, dict):
            if not isinstance(new, dict):
                raise ValueError(f"参数 {path} 应该是一组键值（像 {json.dumps(base, ensure_ascii=False)[:60]}…）")
            for k, v in new.items():
                if k not in base:
                    raise ValueError(f"没有这个参数：{path}.{k}" if path else f"没有这个参数：{k}")
                base[k] = merge(base[k], v, f"{path}.{k}" if path else k)
            return base
        if isinstance(base, list):
            if not isinstance(new, list) or len(new) != len(base) or not all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) for x in new):
                raise ValueError(f"参数 {path} 应该是 {len(base)} 个数的列表，比如 {base}")
            return new
        if isinstance(base, bool) or not isinstance(base, (int, float)):
            return new
        if isinstance(new, bool) or not isinstance(new, (int, float)) or not math.isfinite(new):
            raise ValueError(f"参数 {path} 应该是数字，给的是 {new!r}")
        return int(new) if isinstance(base, int) and float(new).is_integer() else float(new)

    return merge(P, overrides or {}, "")


def reap_interrupted() -> int:
    """服务重启时把上次没跑完的处理标成失败（原来 run.json 永远停在「处理中」、也删不掉，C069）。"""
    n = 0
    d = runs_dir()
    if not d.is_dir():
        return 0
    for p in d.iterdir():
        r = _read(p / "run.json") if p.is_dir() else None
        if r and r.get("status") == "running":
            r.update(status="failed", error="服务重启，这次处理被中断；已经做完的部分留在目录里，要完整结果请重新处理",
                     finished_at=datetime.now().isoformat(timespec="seconds"))
            _dump(p / "run.json", r)
            n += 1
    return n


def check_root(root: str) -> Path:
    p = Path(root or "").expanduser()
    if not str(root or "").strip():
        raise ValueError("先选数据根目录")
    if not p.is_dir():
        raise ValueError(f"不是一个文件夹：{root}")
    rp, out = p.resolve(), runs_dir().resolve()
    if rp == out or out in rp.parents:
        raise ValueError("这是平台自己的 QFLS 输出目录，不能当数据根目录")
    return rp


# ------------------------------------------------------------------ 一帧
def _codes(frames: list[dict], experiments: list[dict]) -> tuple[dict, dict]:
    exp_code = {e["name"]: f"E{i + 1}" for i, e in enumerate(experiments)}
    batch_code: dict[tuple, str] = {}
    for e in experiments:
        for j, b in enumerate(e["batches"]):
            batch_code[(e["name"], b["name"])] = f"B{j + 1}"
    return exp_code, batch_code


def process_frame(fr: dict, P: dict, out_dir: Path | None, title: str) -> tuple[dict, np.ndarray | None]:
    meta = F.read_qvdf(fr.get("qvdf"))
    flags = []
    if fr["problems"]:
        flags.append(M.flag("name_problem", "warn", "；".join(fr["problems"])))
    if not meta["found"]:
        flags.append(M.flag("no_qvdf", "info", "点位下没有 qvdf 元数据"))
    elif not fr["qvdf_name_ok"]:
        flags.append(M.flag("qvdf_name", "info", "qvdf 文件名和点位名不一致"))
    mf = meta.get("fields", {})
    if mf.get("objectives") and fr["objective"] and fr["objective"].upper() not in {o.upper() for o in mf["objectives"]}:
        flags.append(M.flag("meta_mismatch", "warn",
                            f"物镜对不上：文件名写 {fr['objective']}，qvdf 写 {mf['objective']}（按文件名算）"))
    row = {"valid": False, "error": None, "meta": mf, "meta_format": meta.get("format"),
           "score": None, "grade": None, "rank": None}
    small = None
    t0 = time.monotonic()
    try:
        arr, info = F.read_matrix(fr["path"])
        row.update({"rows": info["rows"], "cols": info["cols"], "bom": info["bom"]})
        lo, hi = P["valid_range_ev"]
        arr, rng = F.check_range(arr, lo, hi, P["invalid_out_of_range_frac"])
        row.update(rng)
        if rng["out_of_range_px"]:
            flags.append(M.flag("out_of_range", "warn",
                                f"{rng['out_of_range_frac']:.1%} 的像素在 {lo}–{hi} eV 以外，按中位数填补后再算"))
        px = (mf.get("pixel_size_um_by_objective") or {}).get(fr["objective"] or "")
        if px is None and isinstance(mf.get("pixel_size_um"), float) and len(mf.get("objectives") or [1]) == 1:
            px = mf["pixel_size_um"]
        m, extra = M.compute(arr, P, px)
        flags += m.pop("flags")
        row.update(m)
        row["valid"] = True
        small = R.pool(arr)[0].astype(np.float32)
        if out_dir is not None:
            R.point_figures(out_dir, arr, m, extra, title)
            _dump(out_dir / "extra.json", {k: v for k, v in extra.items() if k != "masks"})
    except F.FrameError as exc:
        row["error"] = str(exc)
        flags.insert(0, M.flag("invalid", "error", f"数据不能用：{exc}"))
    except Exception as exc:          # 一帧出错不能拖垮整批
        row["error"] = f"处理出错：{exc}"
        flags.insert(0, M.flag("invalid", "error", f"处理出错：{exc}"))
    row["flags"] = flags
    row["ms"] = round((time.monotonic() - t0) * 1000)
    return row, small


# ------------------------------------------------------------------ 全局
def _box(vals: list[tuple[float, str]]) -> dict:
    v = np.array([x for x, _ in vals], dtype=float)
    if not len(v):
        return {"n": 0}
    q1, med, q3 = (float(x) for x in np.percentile(v, [25, 50, 75]))
    iqr = q3 - q1
    inside = v[(v >= q1 - 1.5 * iqr) & (v <= q3 + 1.5 * iqr)]
    return {"n": len(v), "q1": q1, "median": med, "q3": q3, "mean": float(v.mean()),
            "std": float(v.std(ddof=1)) if len(v) > 1 else 0.0,
            "min": float(v.min()), "max": float(v.max()),
            "whisker_lo": float(inside.min()), "whisker_hi": float(inside.max()),
            "outliers": [{"y": x, "key": k} for x, k in vals if x < q1 - 1.5 * iqr or x > q3 + 1.5 * iqr]}


def globals_(rows: list[dict], P: dict) -> dict:
    valid = [r for r in rows if r["valid"]]
    keys = [k for k, *_ in M.METRICS]
    # 尺寸：和多数不一样的标出来（像素面积类指标在不同尺寸之间不可比）
    shapes = Counter((r["rows"], r["cols"]) for r in valid)
    if shapes:
        main = shapes.most_common(1)[0][0]
        for r in valid:
            if (r["rows"], r["cols"]) != main:
                r["flags"].append(M.flag("shape_differs", "info",
                                         f"尺寸 {r['rows']} × {r['cols']}，多数帧是 {main[0]} × {main[1]}"))
    # 采集条件：和多数不一样的标出来
    for key, label in (("laser", "激光"), ("intensity_sun", "光强")):
        cnt = Counter(r[key] for r in rows if r.get(key) is not None)
        if len(cnt) > 1:
            common = cnt.most_common(1)[0][0]
            for r in rows:
                if r.get(key) is not None and r[key] != common:
                    r["flags"].append(M.flag("condition_differs", "info", f"{label}是 {r[key]}，多数帧是 {common}"))
    # 组内标准化（实验 × 物镜）+ 组内离群（中位数的稳健 z）
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in valid:
        groups[(r["experiment"], r["objective"] or "?")].append(r)
    gstats = {}
    for g, rs in groups.items():
        gstats["|".join(g)] = st = {}
        for k in keys:
            v = np.array([r[k] for r in rs if r.get(k) is not None], dtype=float)
            st[k] = {"n": len(v), "mean": float(v.mean()) if len(v) else None,
                     "std": float(v.std(ddof=1)) if len(v) > 1 else None}
            for r in rs:
                x = r.get(k)
                s = st[k]["std"]
                r.setdefault("z", {})[k] = (round((x - st[k]["mean"]) / s, 3)
                                            if x is not None and s and len(v) >= 3 else None)
    # 离群：同一 slot（同一处理条件）、同一物镜的平行点之间比中位数
    reps: dict[tuple, list[dict]] = defaultdict(list)
    for r in valid:
        reps[(r["experiment"], r["batch"], r["slot"], r["objective"] or "?")].append(r)
    for rs in reps.values():
        if len(rs) < P["flag_group_min_n"]:
            continue
        med = np.array([r["median"] for r in rs])
        center = float(np.median(med))
        mad = float(np.median(np.abs(med - center))) * 1.4826
        for r in rs:
            rz = (r["median"] - center) / mad if mad > 0 else 0.0
            r["robust_z_median"] = round(rz, 2)
            if abs(rz) > P["flag_group_robust_z"]:
                r["flags"].append(M.flag("group_outlier", "warn",
                                         f"和同一 slot 的 {len(rs) - 1} 个平行点相比中位数离群"
                                         f"（{(r['median'] - center) * 1000:+.0f} meV，稳健 z = {rz:.1f}）"))
    for r in rows:
        lv = {f["level"] for f in r["flags"]}
        r["level"] = "error" if "error" in lv else "warn" if "warn" in lv else "info" if lv else "ok"
        r["anomaly"] = r["level"] in ("error", "warn")
        r["flag_codes"] = [f["code"] for f in r["flags"]]
    # 箱线图 / 分布：按 实验 × 物镜、按物镜
    box = {}
    for k in keys:
        box[k] = {"by_group": {}, "by_objective": {}}
        for g, rs in groups.items():
            box[k]["by_group"]["|".join(g)] = _box([(r[k], r["key"]) for r in rs if r.get(k) is not None])
        for obj in sorted({r["objective"] or "?" for r in valid}):
            box[k]["by_objective"][obj] = _box([(r[k], r["key"]) for r in valid
                                                if (r["objective"] or "?") == obj and r.get(k) is not None])
    ranked = sorted(valid, key=lambda r: (-r["score"], r["code"]))
    by_obj = defaultdict(list)
    for r in ranked:
        by_obj[r["objective"] or "?"].append(r["key"])
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
        r["rank_in_objective"] = by_obj[r["objective"] or "?"].index(r["key"]) + 1
    grades = defaultdict(Counter)
    for r in valid:
        grades["|".join((r["experiment"], r["objective"] or "?"))][r["grade"]] += 1
    return {"group_stats": gstats, "box": box,
            "ranking": [r["key"] for r in ranked],
            "ranking_by_objective": dict(by_obj),
            "grades": {g: dict(c) for g, c in grades.items()},
            "grades_total": dict(Counter(r["grade"] for r in valid)),
            "shapes": {f"{a}×{b}": n for (a, b), n in shapes.items()}}


CSV_COLS = (["rank", "code", "experiment", "batch", "slot", "point", "objective", "laser", "intensity",
             "timestamp", "valid", "score", "grade", "anomaly", "level", "flag_codes", "error",
             "rows", "cols", "pixel_size_um"]
            + [k for k, *_ in M.METRICS if k != "score"]
            + ["low_cc_max_px", "bright_count", "variogram_range_um", "robust_z_median", "file", "key"])


def _csv_cell(v):
    from app.plotting.script_export import guard_formula
    if isinstance(v, list):
        v = ";".join(map(str, v))
    if isinstance(v, float):
        return f"{v:.6g}"
    return guard_formula(v) if isinstance(v, str) else ("" if v is None else v)


def write_csv(path: Path, rows: list[dict]) -> None:
    zcols = [f"z_{k}" for k, *_ in M.METRICS]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS + zcols)
        for r in rows:
            w.writerow([_csv_cell(r.get(c)) for c in CSV_COLS]
                       + [_csv_cell((r.get("z") or {}).get(k)) for k, *_ in M.METRICS])


# ------------------------------------------------------------------ 主流程
def process(root: str | Path, *, run_id: str | None = None, params: dict | None = None, figures: bool = True,
            title: str = "", progress: Callable[[int, int, str], None] | None = None) -> dict:
    root = check_root(str(root))
    P = merge_params(params)
    run_id = run_id or new_run_id()
    out = runs_dir() / run_id
    out.mkdir(parents=True, exist_ok=False)           # 撞号就报错，不把两次处理并进一个目录
    (out / "points").mkdir()
    note = None
    if figures:
        try:
            import matplotlib  # noqa: F401
        except ImportError:   # 出图要 matplotlib；没装的话照样算指标，只是不出图，并说出来
            figures, note = False, "这台电脑没装 matplotlib，这次只算了指标、没出图（装上后重新处理即可）"
    run = {"run_id": run_id, "title": title or root.name, "root": str(root), "status": "running",
           "created_at": datetime.now().isoformat(timespec="seconds"), "figures": figures, "params": P}
    if note:
        run["note"] = note
    _dump(out / "run.json", run)
    t0 = time.monotonic()
    try:
        sc = S.scan(root, exclude=[runs_dir()])
        exp_code, batch_code = _codes(sc["frames"], sc["experiments"])
        for e in sc["experiments"]:
            e["code"] = exp_code[e["name"]]
            for b in e["batches"]:
                b["code"] = batch_code[(e["name"], b["name"])]
        _dump(out / "scan.json", {k: v for k, v in sc.items() if k != "frames"} | {"n_frames": len(sc["frames"])})
        rows, smalls = [], {}
        seen = Counter()
        n = len(sc["frames"])
        for i, fr in enumerate(sc["frames"]):
            if progress:
                progress(i, n, f"{i + 1} / {n} · {fr['rel']}")
            code = (f"{exp_code[fr['experiment']]}-{batch_code[(fr['experiment'], fr['batch'])]}"
                    f"-S{fr['slot_no']}P{fr['point_no']}-{fr['objective'] or '?'}")
            seen[code] += 1
            if seen[code] > 1:
                code += f"-r{seen[code]}"
            pdir = out / "points" / fr["key"]
            pdir.mkdir(exist_ok=True)
            row, small = process_frame(fr, P, pdir if figures else None, code)
            row = {**{k: v for k, v in fr.items() if k not in ("path", "qvdf", "problems")},
                   "code": code, "exp_code": exp_code[fr["experiment"]],
                   "batch_code": batch_code[(fr["experiment"], fr["batch"])], **row}
            rows.append(row)
            if small is not None:
                smalls[fr["key"]] = small
        g = globals_(rows, P)
        # 同一物镜一个色标：对比时颜色才有意义（不同物镜之间仍然不可比）
        scales = {}
        for obj in sorted({r["objective"] or "?" for r in rows if r["valid"]}):
            vs = [r for r in rows if r["valid"] and (r["objective"] or "?") == obj]
            scales[obj] = [float(np.percentile([r["p1"] for r in vs], 5)), float(np.percentile([r["p99"] for r in vs], 95))]
        if figures:
            for r in rows:
                if r["key"] in smalls:
                    lo, hi = scales[r["objective"] or "?"]
                    R.heatmap_fixed(out / "points" / r["key"] / "heatmap_group.png", smalls[r["key"]], lo, hi)
            R.global_figures(out / "global", [r for r in rows if r["valid"]], exp_code)
        for r in rows:
            r = {k: _clean(v) for k, v in r.items()}
            _dump(out / "points" / r["key"] / "metrics.json", r)
        rows = [{k: _clean(v) for k, v in r.items()} for r in rows]
        rows.sort(key=lambda r: (r.get("rank") or 10 ** 6, r["code"]))
        _dump(out / "points.json", rows)
        write_csv(out / "points.csv", rows)
        summary = {
            "run_id": run_id, "root": str(root), "title": run["title"],
            "n_frames": len(rows), "n_valid": sum(r["valid"] for r in rows),
            "n_invalid": sum(not r["valid"] for r in rows), "n_anomaly": sum(r["anomaly"] for r in rows),
            "experiments": [{"name": e["name"], "code": e["code"], "n_frames": e["n_frames"], "n_points": e["n_points"],
                             "batches": [{"name": b["name"], "code": b["code"], "n_frames": b["n_frames"]}
                                         for b in e["batches"]]} for e in sc["experiments"]],
            "objectives": sorted({r["objective"] for r in rows if r["objective"]}),
            "color_scales": scales, "colormap": R.colormap_stops(),
            "metrics": [{"key": k, "label": lab, "unit": u, "cross_objective": x} for k, lab, u, x in M.METRICS],
            "flags": dict(Counter(c for r in rows for c in r["flag_codes"])),
            **g,
        }
        _dump(out / "summary.json", summary)
        (out / "README.md").write_text(readme(summary, P), encoding="utf-8")
        run.update(status="ok", n_frames=len(rows), n_valid=summary["n_valid"], n_anomaly=summary["n_anomaly"],
                   n_experiments=len(sc["experiments"]), seconds=round(time.monotonic() - t0, 1),
                   finished_at=datetime.now().isoformat(timespec="seconds"))
        if progress:
            progress(n, n, "完成")
        return run
    except BaseException as exc:
        run.update(status="cancelled" if isinstance(exc, tasks.Cancelled) else "failed", error=str(exc)[:2000],
                   finished_at=datetime.now().isoformat(timespec="seconds"))
        raise
    finally:
        _dump(out / "run.json", run)


def readme(summary: dict, P: dict) -> str:
    met = "\n".join(f"| `{m['key']}` | {m['label']} | {m['unit'] or '—'} | {'能' if m['cross_objective'] else '**不能**'} |"
                    for m in summary["metrics"])
    exps = "\n".join(f"- {e['code']} = {e['name']}（{e['n_frames']} 帧）" for e in summary["experiments"])
    return f"""# QFLS 处理结果 {summary['run_id']}

数据根目录：`{summary['root']}`（**只读**，平台没有改动其中任何文件）

{summary['n_frames']} 帧，其中有效 {summary['n_valid']}、无效 {summary['n_invalid']}、有异常标记 {summary['n_anomaly']}。

## 编号
{exps}

样点编号 `E1-B1-S2P3-20X` = 实验 E1、批次 B1、slot2、point3、20X 物镜；同一点同一物镜多帧时后缀 `-r2`。

## 文件
- `points.json` / `points.csv`：每个样点一行（排名、身份、全部指标、分、等级、异常、组内 z）
- `summary.json`：分组统计（实验 × 物镜）、箱线图数据、排名、等级分布、色标
- `scan.json`：认出的实验 / 批次、跳过的目录和文件（带原因）、修过的乱码名
- `points/<key>/`：`metrics.json`、`extra.json`（直方图、剖面、变异函数的数据）和
  `heatmap.png`、`hist.png`、`profile.png`、`anomaly.png`、`variogram.png`、`summary.png`、`heatmap_group.png`
- `global/`：`boxplots.png`、`ranking.png`、`grades.png`

## 指标（能不能跨物镜比）
| 键 | 含义 | 单位 | 跨物镜 |
|---|---|---|---|
{met}

`z_*` 列 = 同实验、同物镜组内的 (x − 组均值) / 组标准差（组内少于 3 个有效点时为空）。

## 使用限制（必须遵守）
- **不同物镜的绝对 QFLS 和面积占比不能直接比较**：比较只在同一物镜内做，或者用组内 z。
- 只有单一光强（1 sun）：**不做伪 JV、理想因子、PCE、FF**。
- Moran's I ≈ 0 表示空间随机；> {P['moran_threshold']} 提示结构不均（在 {P['moran_grid'][0]} × {P['moran_grid'][1]} 的块均值网格上算）。
- 综合质量分、ABCD 等级、异常阈值是平台定的（见 run.json 里的 params），不是标准，换了阈值结论会变。
- 无效帧（行列不一致、缺失、超范围过多）不打分，不进排名和分布。
"""


# ------------------------------------------------------------------ 读
def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def list_runs() -> list[dict]:
    d = runs_dir()
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        r = _read(p / "run.json") if p.is_dir() else None
        if r:
            r.pop("params", None)
            out.append(r)
    return sorted(out, key=lambda r: r.get("created_at", ""), reverse=True)


def run_path(run_id: str) -> Path:
    if not run_id.startswith("qr_") or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise KeyError(run_id)
    p = runs_dir() / run_id
    if not (p / "run.json").is_file():
        raise KeyError(run_id)
    return p


def load(run_id: str, name: str):
    return _read(run_path(run_id) / name)


def delete_run(run_id: str) -> None:
    shutil.rmtree(run_path(run_id))
