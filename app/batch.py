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
    # 同事装的模块，各自的控件值：{module_id: {control_key: value}}。
    # 不填就用模块声明里的默认值。
    module_params: dict = field(default_factory=dict)

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
    # 已装模块声明的曲线：{列名: 数组}。同事的模块产出什么，这里就多什么列。
    #   module_curves —— 跟着抽稀过的时间轴 out.t
    #   full_curves   —— 跟着未抽稀的 ot_t（声明了 batch_all_frames 的模块）
    module_curves: dict[str, np.ndarray] = field(default_factory=dict)
    full_curves: dict[str, np.ndarray] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------ 模块驱动的曲线
#
# 「批处理产出哪些曲线」不再写死在这里，而是由已装模块的 batch_curves 声明决定。
# 同事放一个模块进 workspace/modules/，它的曲线就自动出现在长表、对比页叠图、
# 时刻切片和导出脚本里 —— 这个文件一个字都不用改。

# 老配方的扁平字段 → 特殊处理模块的控件。
# 界面上存了几个月的配方不能因为迁移就读不出来了。
_LEGACY_MAP = {
    "thickness.fringe_ot": {
        "band": lambda r: [r.band_min, r.band_max],
    },
    "special.slope_integral": {
        "integ": lambda r: [r.integral_min, r.integral_max],
        "slope_center": lambda r: r.slope_center,
        "slope_half": lambda r: r.slope_half_width,
    },
}


def _params_for(module_id: str, spec, recipe: "Recipe") -> dict:
    """一个模块这一次批处理用什么参数：默认值 ← 老配方映射 ← 显式指定。"""
    params = dict(spec.defaults())
    for key, pick in (_LEGACY_MAP.get(module_id) or {}).items():
        try:
            params[key] = pick(recipe)
        except AttributeError:
            pass
    params.update((recipe.module_params or {}).get(module_id) or {})
    return params


def _run_modules(lam, M, t, recipe: "Recipe", warnings: list[str], *,
                 all_frames: bool = False, meta: dict | None = None,
                 artifact_id: str = "") -> dict[str, np.ndarray]:
    """跑声明了 batch_curves 的模块，收集它们的曲线。

    `all_frames` 选的是哪一批模块：False = 跟着抽稀后的时间轴那些，
    True = 声明了 `batch_all_frames` 要拿全部帧的那些（膜厚）。
    两批分开跑、分开存 —— 时间轴不是同一条。

    **一个模块崩了不能带走整批。** 记一条 warning，那几列留空 ——
    同事的模块出问题，你的其它结果照常拿得到。
    """
    from app.modules.base import ModuleContext
    from app.modules.registry import registry as module_registry

    # registry 平时由 web 启动时装好。但批处理跑在任务线程里，也可能被脚本或
    # 测试直接调起来 —— 那些路径没有走过 lifespan。空着就懒加载一次，
    # 否则症状是「批处理静默地一条曲线都不产出」，最难查的那种。
    if not module_registry.all():
        module_registry.load_all()

    out: dict[str, np.ndarray] = {}
    for mod in module_registry.all():
        spec = mod.spec
        if not spec.batch_curves or bool(spec.batch_all_frames) != all_frames:
            continue
        params = _params_for(spec.id, spec, recipe)
        try:
            panels = mod.compute(ModuleContext(lam, M, t, params, meta=meta,
                                               artifact_id=artifact_id))
        except Exception as exc:                     # noqa: BLE001
            warnings.append(f"模块「{spec.name}」算不出来：{type(exc).__name__}: {exc}")
            continue
        for cv in spec.batch_curves:
            d = panels.get(cv.from_panel)
            if d is None:
                warnings.append(f"模块「{spec.name}」没有返回面板 {cv.from_panel}")
                continue
            col = d.column(cv.key)
            if col is None:
                warnings.append(
                    f"模块「{spec.name}」的面板 {cv.from_panel} 里没有 "
                    f"batch_extra[\"{cv.key}\"]，{cv.name} 这一列跳过"
                    if cv.key else
                    f"模块「{spec.name}」的面板 {cv.from_panel} 没画出任何数据，"
                    f"{cv.name} 这一列跳过")
                continue
            y = np.asarray([np.nan if v is None else float(v) for v in col],
                           dtype=np.float32)
            if len(y) != len(t):
                warnings.append(
                    f"模块「{spec.name}」的 {cv.name} 有 {len(y)} 个点，"
                    f"时间轴有 {len(t)} 帧，对不上，这一列跳过")
                continue
            if np.all(np.isnan(y)):
                warnings.append(
                    f"模块「{spec.name}」的 {cv.name} 全是空值 —— "
                    f"多半是参数落在数据范围（{lam[0]:g}–{lam[-1]:g} nm）之外")
            out[cv.name] = y
    return out


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

        # 曲线由**已装的模块**产出 —— 模块声明了 batch_curves，
        # 平台就自动把它接进长表、对比页叠图、时刻切片和导出脚本。
        # 同事加一个模块，这些地方一处都不用改。
        out.module_curves = _run_modules(lam, M, t, recipe, out.warnings,
                                         meta=sm.meta,
                                         artifact_id=row["matrix_id"])
        integ = out.module_curves.get("integral")
        slope = out.module_curves.get("slope")
        if integ is None:
            integ = np.full(len(t), np.nan)
        if slope is None:
            slope = np.full(len(t), np.nan)

        # 分辨率诊断是纯几何量。用 fringe_ot 那一份实现，别在这儿抄第二遍 ——
        # 抄第二遍就会有一天两边对不上。
        if lam[0] <= recipe.band_min and recipe.band_max <= lam[-1]:
            from app.analysis import fringe_ot

            diag = fringe_ot.diagnostics_for(recipe.band_min, recipe.band_max)
            out.metrics["ot_floor"] = diag["ot_floor_nm"]
            out.metrics["fringe_bin"] = diag["bin_f_nm"]
        else:
            out.warnings.append(
                f"膜厚窗口 {recipe.band_min:g}–{recipe.band_max:g} nm 超出数据范围")

        # 逐帧的量（膜厚）走**未抽样**的 sm.lam / sm.t / sm.M：
        # 上面那个 max_time_points 只影响 integral / slope 的显示密度，
        # 而膜厚是要看干燥过程哪一秒变坏的，抽稀等于把答案抹掉。
        #
        # 这里跑的是**和单样品页同一个模块** —— 批处理里另写一份 FFT 的话，
        # 两个页面对同一个样品会给出不同的膜厚，而且没人知道该信哪个。
        out.full_curves = _run_modules(sm.lam, sm.M, sm.t, recipe, out.warnings,
                                       all_frames=True, meta=sm.meta,
                                       artifact_id=row["matrix_id"])
        if out.full_curves:
            out.ot_t = np.asarray(sm.t, dtype=np.float32)
        ot = out.full_curves.get("ot")
        ot_ok = out.full_curves.get("ot_ok")
        if ot is not None and ot_ok is not None:
            good = np.isfinite(ot) & (ot_ok > 0.5)
            n_ok = int(good.sum())
            out.metrics["ot_ok_frames"] = float(n_ok)
            out.metrics["ot_frames"] = float(len(ot))
            if n_ok:
                vals = ot[good]
                out.metrics["ot_first_ok"] = float(vals[0])
                out.metrics["ot_last_ok"] = float(vals[-1])
            else:
                out.warnings.append(
                    f"膜厚：{len(ot)} 帧里没有一帧达到「可信」"
                    f"（窗口 {recipe.band_min:g}–{recipe.band_max:g} nm 下条纹数不足）")

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
        cols = {
            "sample_id": o.sample_id,
            "sample_name": o.sample_name,
            "batch": o.batch or "",
            "t": o.t,
        }
        # 模块声明了几条曲线就有几列。integral / slope 也走这条路 ——
        # 它们现在也是模块产出的，不是平台写死的。
        for name, y in (o.module_curves or {}).items():
            cols[name] = y
        cols.setdefault("integral", o.integral)
        cols.setdefault("slope", o.slope)
        frames.append(pd.DataFrame(cols))
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    return tabular.write_table(parent_id, "batch_curves", df)


def _write_thickness_table(parent_id: str, outcomes: list[SampleOutcome]) -> dict:
    """膜厚长表。和 batch_curves 分开存 —— 时间轴不是同一条，见 SampleOutcome.ot_t。"""
    import pandas as pd

    frames = []
    for o in outcomes:
        if not o.ok or o.ot_t is None or not o.full_curves:
            continue
        cols = {
            "sample_id": o.sample_id,
            "sample_name": o.sample_name,
            "batch": o.batch or "",
            "t": o.ot_t,
        }
        # 列名由模块的 batch_curves 决定，这里不写死 —— 膜厚模块出 ot / ot_ok，
        # 同事的全帧模块出别的，都自动多出几列。
        for name, y in o.full_curves.items():
            if len(y) == len(o.ot_t):
                cols[name] = y
        frames.append(pd.DataFrame(cols))
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
