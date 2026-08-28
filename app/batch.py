"""批处理：把单样品页调好的那套参数，套到一批样品上。

**配方就是当前视图。** 用户已经在单样品页把波段、积分区间、斜率波长调对了，
那套参数直接就是配方 —— 不做第二套参数表单。

存储形态（这是这一层最重要的三个决定）：

1. **父运行 + 每样品一条子运行。** 上千个样品里一定有跑失败的，你必须知道
   是哪些、为什么。一条大记录说不清楚。

2. **曲线进一张长表 Parquet，不是 N 个文件。**
   `sample_id | sample_name | batch | t | ot | integral | slope`
   一次读取就能画叠图，DuckDB 直接可查，加样品就是加行。

3. **标量进 key_result。** 这是那张长表设计的兑现时刻 —— 批处理跑完，
   构效关系页立刻就有东西可用，不需要任何额外工作。
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app import config, tasks
from app.parsers import matrix, render
from app.storage import artifacts, db, results, selection, tabular

BATCH_SKILL_ID = "batch.abs_thickness"
BATCH_VERSION = "0.2.0"

# 每个样品的计算是 numpy 主导的，会放开 GIL，所以线程池是有效的。
# 数据库写入不并行 —— 全部在主任务线程里做，避免写锁竞争。
COMPUTE_WORKERS = 4


@dataclass
class Recipe:
    """一次批处理的配方。字段和单样品页的控件一一对应。"""
    band_min: float = 780.0          # 膜厚窗口
    band_max: float = 1050.0
    integral_min: float = 800.0      # 波段积分
    integral_max: float = 950.0
    slope_center: float = 950.0      # 谱斜率
    slope_half_width: float = 10.0
    max_time_points: int = 0         # 0 = 全部帧

    @classmethod
    def from_dict(cls, d: dict | None) -> "Recipe":
        d = d or {}
        r = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__
                   and v is not None})
        if r.band_max <= r.band_min:
            raise ValueError(f"膜厚窗口不合法：{r.band_min}–{r.band_max} nm")
        if r.integral_max <= r.integral_min:
            raise ValueError(f"积分波段不合法：{r.integral_min}–{r.integral_max} nm")
        if r.slope_half_width <= 0:
            raise ValueError("斜率窗口半宽必须大于 0")
        return r

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class SampleOutcome:
    sample_id: str
    sample_name: str
    batch: str | None
    artifact_id: str
    ok: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    t: np.ndarray | None = None
    integral: np.ndarray | None = None
    slope: np.ndarray | None = None
    # 光学厚度：每帧一个值 + 每帧一个判级。判级要跟着一起存 ——
    # 「1s 内的平均膜厚」光有均值没法判断可不可信，那个数就没法用。
    #
    # 自带一条时间轴：膜厚拿的是**全部帧**，而 integral / slope 可能被
    # max_time_points 抽稀过。硬塞进同一张表就得二选一：要么给膜厚抽稀
    # （上一轮刚说过不行），要么给另外两条插值（凭空造数）。所以分两张表。
    ot_t: np.ndarray | None = None
    ot: np.ndarray | None = None
    ot_status: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------ 单样品
def _process_one(row: dict, recipe: Recipe) -> SampleOutcome:
    """一个样品的计算。不碰数据库 —— 写入统一由主线程做。"""
    out = SampleOutcome(sample_id=row["sample_id"], sample_name=row["name"],
                        batch=row.get("batch"), artifact_id=row["matrix_id"])
    try:
        path = artifacts.local_path(row["matrix_id"])
        sm = matrix.load_cached(path)

        lam, M, t = sm.lam, sm.M, sm.t
        if recipe.max_time_points and len(t) > recipe.max_time_points:
            keep = render.pick_frames(t, recipe.max_time_points)
            t, M = t[keep], M[:, keep]

        integ = render.band_integral(M, lam, recipe.integral_min, recipe.integral_max)
        slope = render.wavelength_slope(M, lam, recipe.slope_center,
                                        recipe.slope_half_width)

        if np.all(np.isnan(integ)):
            out.warnings.append(
                f"积分波段 {recipe.integral_min:g}–{recipe.integral_max:g} nm "
                f"落在数据范围 {lam[0]:g}–{lam[-1]:g} nm 之外")
        if np.all(np.isnan(slope)):
            out.warnings.append(
                f"斜率窗口 {recipe.slope_center:g}±{recipe.slope_half_width:g} nm "
                f"落在数据范围之外")

        # 分辨率诊断是纯几何量。用 fringe_ot 那一份实现，别在这儿抄第二遍 ——
        # 抄第二遍就会有一天两边对不上。
        if lam[0] <= recipe.band_min and recipe.band_max <= lam[-1]:
            from app.analysis import fringe_ot

            diag = fringe_ot.diagnostics_for(recipe.band_min, recipe.band_max)
            out.metrics["ot_floor"] = diag["ot_floor_nm"]
            out.metrics["fringe_bin"] = diag["bin_f_nm"]

            # 逐帧光学厚度。走的是单样品页那条**同一个** extract_series ——
            # 批处理里另写一份 FFT 的话，两个页面对同一个样品会给出不同的膜厚，
            # 而且没人知道该信哪个。
            #
            # 注意用的是**未抽样**的 lam / sm.M：膜厚必须拿完整光谱算，
            # 上面那个 max_time_points 只影响 integral / slope 的显示密度。
            try:
                res = fringe_ot.extract_series(
                    sm.lam, sm.t, sm.M,
                    target_times_s="all",
                    window_nm=[recipe.band_min, recipe.band_max],
                    accurate_cycles=fringe_ot.PLATFORM_ACCURATE_CYCLES,
                    input_is_absorbance=bool(sm.meta.get("input_is_absorbance", False)),
                )
                pts = res["points"]
                out.ot_t = np.array([q["t"] for q in pts], dtype=np.float32)
                out.ot = np.array([q["ot_nm"] for q in pts], dtype=np.float32)
                out.ot_status = [q["status"] for q in pts]
                n_ok = sum(1 for q in pts if q["status"] == "OK")
                out.metrics["ot_ok_frames"] = float(n_ok)
                out.metrics["ot_frames"] = float(len(pts))
                if n_ok:
                    ok_vals = [q["ot_nm"] for q in pts if q["status"] == "OK"]
                    out.metrics["ot_first_ok"] = float(ok_vals[0])
                    out.metrics["ot_last_ok"] = float(ok_vals[-1])
                else:
                    out.warnings.append(
                        f"膜厚：{len(pts)} 帧里没有一帧达到「可信」"
                        f"（窗口 {recipe.band_min:g}–{recipe.band_max:g} nm 下条纹数不足）")
            except Exception as exc:            # noqa: BLE001
                # 膜厚算不出来不该把整个样品判成失败 —— integral / slope 还是好的。
                # 记一条 warning，界面上那一列显示空白而不是假数。
                out.warnings.append(f"膜厚算不出来：{exc}")
        else:
            out.warnings.append(
                f"膜厚窗口 {recipe.band_min:g}–{recipe.band_max:g} nm 超出数据范围")

        out.t = np.asarray(t, dtype=np.float32)
        out.integral = np.asarray(integ, dtype=np.float32)
        out.slope = np.asarray(slope, dtype=np.float32)

        finite = integ[np.isfinite(integ)]
        if finite.size:
            out.metrics.update({
                "integral_initial": float(finite[0]),
                "integral_final": float(finite[-1]),
                "integral_min": float(finite.min()),
                "integral_max": float(finite.max()),
            })
            if finite[0] != 0:
                out.metrics["integral_ratio"] = float(finite[-1] / finite[0])
        fs = slope[np.isfinite(slope)]
        if fs.size:
            out.metrics["slope_abs_max"] = float(np.abs(fs).max())

        out.metrics["n_time"] = float(len(t))
        out.metrics["n_lambda"] = float(len(lam))
        out.ok = True
    except Exception as exc:
        out.error = str(exc)
    return out


# ------------------------------------------------------------------ 批处理
@tasks.register(BATCH_SKILL_ID)
def run_batch(ctx: tasks.TaskContext) -> dict:
    """任务执行体。参数：{filter, recipe, title}"""
    flt = selection.normalize(ctx.params.get("filter") or {})
    recipe = Recipe.from_dict(ctx.params.get("recipe"))

    page = selection.page({**flt, "has_matrix": True}, limit=100000)
    rows = [r for r in page["rows"] if r.get("matrix_id")]
    total = len(rows)
    if not total:
        raise ValueError("筛选式没有命中任何带光谱矩阵的样品")

    parent_id = results.start_run(
        skill_id=BATCH_SKILL_ID, skill_version=BATCH_VERSION,
        skill_name="批处理 · 吸收光谱与膜厚",
        params={"recipe": recipe.as_dict(), "filter": flt, "n_samples": total,
                "title": (ctx.params.get("title") or "").strip()},
        source="skill")

    ctx.progress(0, total, f"准备处理 {total} 个样品")

    outcomes: list[SampleOutcome] = []
    n_ok = n_failed = 0
    cancelled = False

    # numpy 会放开 GIL，所以计算并行有效；数据库写入留在这个线程里做
    with ThreadPoolExecutor(max_workers=COMPUTE_WORKERS) as pool:
        futures = {pool.submit(_process_one, r, recipe): r for r in rows}
        for i, fut in enumerate(as_completed(futures), start=1):
            if ctx.is_cancelled:
                cancelled = True
                for f in futures:
                    f.cancel()
                break
            out = fut.result()
            outcomes.append(out)
            if out.ok:
                n_ok += 1
                _write_child(parent_id, out, recipe)
            else:
                n_failed += 1
                _write_child(parent_id, out, recipe)
            if i % 5 == 0 or i == total:
                ctx.progress(i, total, f"{out.sample_name}（{n_ok} 成功 / {n_failed} 失败）")
                ctx.tally(n_ok, n_failed)

    ctx.tally(n_ok, n_failed)

    table_meta = _write_long_table(parent_id, outcomes) if n_ok else None
    ot_meta = _write_thickness_table(parent_id, outcomes) if n_ok else None
    results.finish_run(
        parent_id, "cancelled" if cancelled else ("ok" if n_ok else "failed"),
        warnings=[f"{n_failed} 个样品失败"] if n_failed else [],
        error=None if n_ok else "全部样品都失败了")

    if cancelled:
        raise tasks.Cancelled()

    return {
        "parent_run_id": parent_id,
        "n_total": total, "n_ok": n_ok, "n_failed": n_failed,
        "table": table_meta,
        "thickness_table": ot_meta,
        "recipe": recipe.as_dict(),
        "filter": flt,
    }


def _write_child(parent_id: str, out: SampleOutcome, recipe: Recipe) -> None:
    """每个样品一条子运行 —— 失败的也要留痕，否则你不知道是哪些没跑成。"""
    run_id = results.start_run(
        skill_id=BATCH_SKILL_ID, skill_version=BATCH_VERSION,
        skill_name="批处理 · 单样品",
        params=recipe.as_dict(), inputs=[out.artifact_id],
        sample_id=out.sample_id, source="skill")
    with db.tx() as c:
        c.execute("UPDATE analysis_run SET parent_run_id=? WHERE analysis_run_id=?",
                  (parent_id, run_id))

    if out.ok and out.metrics:
        results.write_results(
            run_id,
            [{"field_name": k, "value": v, "unit": _UNITS.get(k, ""),
              "label": _LABELS.get(k, k), "quality": "validated"}
             for k, v in out.metrics.items() if math.isfinite(v)],
            sample_id=out.sample_id, version=BATCH_VERSION)

    results.finish_run(run_id, "ok" if out.ok else "failed",
                       warnings=out.warnings, error=out.error or None)


_UNITS = {
    "ot_floor": "nm", "fringe_bin": "nm",
    "integral_initial": "a.u.·nm", "integral_final": "a.u.·nm",
    "integral_min": "a.u.·nm", "integral_max": "a.u.·nm",
    "slope_abs_max": "a.u./nm",
}
_LABELS = {
    "ot_floor": "窗口可测最小光学厚度", "fringe_bin": "一个频率 bin",
    "integral_initial": "积分初值", "integral_final": "积分终值",
    "integral_ratio": "积分终/初比", "integral_min": "积分最小",
    "integral_max": "积分最大", "slope_abs_max": "斜率绝对值峰",
    "n_time": "帧数", "n_lambda": "波长点数",
}


def _write_long_table(parent_id: str, outcomes: list[SampleOutcome]) -> dict:
    """所有样品的曲线合成一张长表。

    N 个文件的话，画叠图要开 N 次；长表一次读完，而且 DuckDB 能直接查。
    """
    import pandas as pd

    frames = []
    for o in outcomes:
        if not o.ok or o.t is None:
            continue
        frames.append(pd.DataFrame({
            "sample_id": o.sample_id,
            "sample_name": o.sample_name,
            "batch": o.batch or "",
            "t": o.t,
            "integral": o.integral,
            "slope": o.slope,
        }))
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    return tabular.write_table(parent_id, "batch_curves", df)


def _write_thickness_table(parent_id: str, outcomes: list[SampleOutcome]) -> dict:
    """膜厚长表。和 batch_curves 分开存 —— 时间轴不是同一条，见 SampleOutcome.ot_t。"""
    import pandas as pd

    frames = []
    for o in outcomes:
        if not o.ok or o.ot is None or o.ot_t is None:
            continue
        frames.append(pd.DataFrame({
            "sample_id": o.sample_id,
            "sample_name": o.sample_name,
            "batch": o.batch or "",
            "t": o.ot_t,
            "ot": o.ot,
            "status": o.ot_status,
        }))
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    return tabular.write_table(parent_id, "batch_thickness", df)


# ------------------------------------------------------------------ 读取结果
def batch_detail(parent_run_id: str) -> dict:
    run = db.query_one("SELECT * FROM analysis_run WHERE analysis_run_id=?",
                       (parent_run_id,))
    if not run:
        raise KeyError(f"没有这次批处理：{parent_run_id}")
    try:
        run["params"] = json.loads(run.get("params_json") or "{}")
    except json.JSONDecodeError:
        run["params"] = {}

    children = db.query(
        "SELECT r.analysis_run_id, r.sample_id, r.status, r.error, r.warnings_json,"
        "       s.name AS sample_name, s.batch,"
        "       (SELECT COUNT(*) FROM key_result k"
        "         WHERE k.analysis_run_id = r.analysis_run_id) AS n_results"
        " FROM analysis_run r LEFT JOIN sample s ON s.sample_id = r.sample_id"
        " WHERE r.parent_run_id = ? ORDER BY s.name", (parent_run_id,))
    for ch in children:
        try:
            ch["warnings"] = json.loads(ch.pop("warnings_json") or "[]")
        except json.JSONDecodeError:
            ch["warnings"] = []

    return {
        "run": run,
        "children": children,
        "n_ok": sum(1 for c in children if c["status"] == "ok"),
        "n_failed": sum(1 for c in children if c["status"] == "failed"),
        "tables": tabular.tables_for_run(parent_run_id),
    }


def recent_batches(limit: int = 20) -> list[dict]:
    return db.query(
        "SELECT r.analysis_run_id, r.status, r.started_at, r.finished_at, r.params_json,"
        "       (SELECT COUNT(*) FROM analysis_run c"
        "         WHERE c.parent_run_id = r.analysis_run_id) AS n_children,"
        "       (SELECT COUNT(*) FROM analysis_run c"
        "         WHERE c.parent_run_id = r.analysis_run_id AND c.status='failed')"
        "         AS n_failed"
        " FROM analysis_run r WHERE r.skill_id = ? AND r.parent_run_id IS NULL"
        # started_at 只到秒。同一秒里跑的两次会并列，没有第二个排序键的话
        # 历史列表的顺序就是随机的 —— rowid 是插入顺序，拿来兜底。
        " ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?", (BATCH_SKILL_ID, limit))
