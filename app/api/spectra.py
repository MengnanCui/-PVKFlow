"""光谱矩阵的视图接口。

分工（见 app/parsers/render.py 顶部）：
  * 位图（热力图、条纹图）走 /heatmap.png —— 服务端渲染，浏览器当图片缓存
  * 曲线走 /frames —— 只给数值，前端画矢量图并在本地实时重算

/frames 返回的抽样谱是「特殊处理」的数据源：拿到之后换积分波段、换斜率波长
都在前端完成，0 延迟。它同时也喂「归一化强度 vs 波长」那张图。
"""
from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Query, Response

from app.api.common import ApiError, guard
from app.parsers import matrix, render
from app.storage import artifacts

router = APIRouter(prefix="/api/spectra", tags=["spectra"])

# 前端持有的抽样谱预算（float32 字节数）。超过就降波长分辨率，
# 但不低于 2nm —— 再粗积分边界和斜率窗口就失去意义了。
# 1 MB 对本地服务是几十毫秒的事，对 1501×601 的矩阵刚好落在 2nm。
FRAMES_BUDGET_BYTES = 1_000_000
MIN_LAMBDA_STEP_NM = 2.0


def _load(artifact_id: str) -> matrix.SpectralMatrix:
    row = artifacts.get(artifact_id)
    if not row:
        raise ApiError(f"文件不存在：{artifact_id}", 404, "not_found")
    path = guard(artifacts.local_path, artifact_id)
    try:
        return matrix.load_cached(path, row.get("sha256"))
    except ValueError as exc:
        raise ApiError(str(exc), 400, "not_a_matrix") from exc
    except Exception as exc:
        raise ApiError(f"解析光谱矩阵失败：{exc}", 400, "parse_failed") from exc


@router.get("/{artifact_id}/meta")
def meta(artifact_id: str) -> dict:
    """矩阵尺寸、采样率、值域，外加它属于哪个样品。界面上把这些如实显示出来 ——
    用户第一次跑就知道自己的数据到底多大。"""
    from app.storage import db

    sm = _load(artifact_id)
    d = sm.describe()

    row = artifacts.get(artifact_id) or {}
    d["artifact_id"] = artifact_id
    d["filename"] = row.get("filename")
    d["display_path"] = row.get("display_path")
    d["file_size"] = row.get("size")
    d["sample"] = db.query_one(
        "SELECT sample_id, name, batch FROM sample WHERE sample_id = ?",
        (row.get("sample_id"),)) if row.get("sample_id") else None
    step = _lambda_step_for_budget(sm)
    d["frames_lambda_step"] = step
    d["frames_bytes_estimate"] = int(
        np.ceil(len(sm.lam) / max(1, round(step / max(d["lambda_step"], 1e-9))))
        * len(sm.t) * 4)
    return d


def _lambda_step_for_budget(sm: matrix.SpectralMatrix) -> float:
    """自适应决定抽样谱的波长步长。不写死参数——数据多大就抽多少。"""
    L, T = sm.M.shape
    native = float(np.median(np.diff(sm.lam))) if L > 1 else 1.0
    if native <= 0:
        return MIN_LAMBDA_STEP_NM
    stride = max(1, int(np.ceil(L * T * 4 / FRAMES_BUDGET_BYTES)))
    return max(native * stride, min(MIN_LAMBDA_STEP_NM, native))


def _frames_arrays(sm: matrix.SpectralMatrix, lam_min, lam_max, max_time_points):
    """抽样后的 (lam, t, M)。JSON 与二进制两个端点共用，保证两边一致。"""
    lam, M = sm.lam, sm.M
    if lam_min is not None or lam_max is not None:
        mask = np.ones(len(lam), bool)
        if lam_min is not None:
            mask &= lam >= lam_min
        if lam_max is not None:
            mask &= lam <= lam_max
        if mask.sum() < 4:
            raise ApiError("这个波段里点数太少", 400, "band_too_narrow")
        lam, M = lam[mask], M[mask]

    native = float(np.median(np.diff(lam))) if len(lam) > 1 else 1.0
    step = _lambda_step_for_budget(sm)
    stride = max(1, int(round(step / native))) if native > 0 else 1
    lam, M = lam[::stride], M[::stride]

    t = sm.t
    if max_time_points and len(t) > max_time_points:
        keep = render.pick_frames(t, max_time_points)
        t, M = t[keep], M[:, keep]
    return lam, t, np.ascontiguousarray(M, dtype=np.float32), native * stride, native


@router.get("/{artifact_id}/frames")
def frames(
    artifact_id: str,
    lam_min: float | None = None,
    lam_max: float | None = None,
    max_time_points: int = Query(0, ge=0, le=20000),
) -> dict:
    """抽样光谱的**坐标轴与形状**，数值走 data_url 指向的二进制端点。

    为什么分两个请求：751×601 的矩阵编码成 JSON 是 6.7 MB，同样的数据
    float32 二进制只要 1.8 MB。本地服务下二进制几十毫秒就到，JSON 要几百毫秒
    而且解析还要再花一笔。

    前端拿到这份数据后，换积分波段、换斜率波长、换叠谱时刻全部在本地完成，
    0 延迟 —— 这就是"特殊处理用光谱处理好的数据"的落地方式。
    """
    sm = _load(artifact_id)
    lam, t, M, step, native = _frames_arrays(sm, lam_min, lam_max, max_time_points)

    params = []
    if lam_min is not None:
        params.append(f"lam_min={lam_min}")
    if lam_max is not None:
        params.append(f"lam_max={lam_max}")
    if max_time_points:
        params.append(f"max_time_points={max_time_points}")
    query = ("?" + "&".join(params)) if params else ""

    return {
        "lambda": [round(float(x), 4) for x in lam],
        "time": [round(float(x), 4) for x in t],
        # 数值是 float32 小端、行=波长 列=时间、行主序，共 L×T 个
        "data_url": f"/api/spectra/{artifact_id}/frames.bin{query}",
        "dtype": "float32",
        "layout": "lambda_major",
        "shape": [int(M.shape[0]), int(M.shape[1])],
        "bytes": int(M.nbytes),
        "lambda_step": round(float(step), 4),
        "native_lambda_step": round(float(native), 4),
        "downsampled": bool(step > native * 1.001 or len(t) < len(sm.t)),
        "source_shape": [int(sm.M.shape[0]), int(sm.M.shape[1])],
    }


@router.get("/{artifact_id}/frames.bin")
def frames_bin(
    artifact_id: str,
    lam_min: float | None = None,
    lam_max: float | None = None,
    max_time_points: int = Query(0, ge=0, le=20000),
) -> Response:
    """抽样谱的数值，float32 小端、行主序（行=波长）。配合 /frames 使用。"""
    sm = _load(artifact_id)
    _, _, M, _, _ = _frames_arrays(sm, lam_min, lam_max, max_time_points)
    return Response(
        content=M.astype("<f4").tobytes(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=600",
                 "X-Shape": f"{M.shape[0]}x{M.shape[1]}"},
    )


@router.get("/{artifact_id}/heatmap.png")
def heatmap(
    artifact_id: str,
    axis: str = Query("wavelength", pattern="^(wavelength|wavenumber)$"),
    lam_min: float | None = None,
    lam_max: float | None = None,
    norm: str = Query("frame", pattern="^(none|frame|global|wavelength)$"),
    cmap: str = Query("ice"),
    width: int = Query(1100, ge=120, le=3000),
    height: int = Query(560, ge=120, le=3000),
) -> Response:
    """热力图 / 条纹图。

    这张图是位图，不是"偷懒的死图"——10⁶ 个 SVG 矩形会卡死浏览器，
    而同一张图 PNG 只要几百 KB。坐标轴由前端用矢量画在图外面。
    """
    sm = _load(artifact_id)
    lam, M = sm.lam, sm.M

    if lam_min is not None or lam_max is not None:
        mask = np.ones(len(lam), bool)
        if lam_min is not None:
            mask &= lam >= lam_min
        if lam_max is not None:
            mask &= lam <= lam_max
        if mask.sum() < 8:
            raise ApiError(f"波段 {lam_min}–{lam_max} nm 内只有 {int(mask.sum())} 个点",
                           400, "band_too_narrow")
        lam, M = lam[mask], M[mask]

    if axis == "wavenumber":
        k, M = render.to_wavenumber(M, lam)
        y_min, y_max = float(k[0]), float(k[-1])
    else:
        y_min, y_max = float(lam[0]), float(lam[-1])

    png, info = render.render_png(M, cmap=cmap, norm=norm,
                                  max_width=width, max_height=height)

    # HTTP header 只能是 latin-1，所以这里只放数字，坐标轴文字由前端给。
    headers = {
        "Cache-Control": "public, max-age=600",
        "Access-Control-Expose-Headers": "X-Axis-Y-Min,X-Axis-Y-Max,X-Axis-X-Min,"
                                         "X-Axis-X-Max,X-Render-Info",
        "X-Axis-Y-Min": f"{y_min:.8g}", "X-Axis-Y-Max": f"{y_max:.8g}",
        "X-Axis-X-Min": f"{float(sm.t[0]):.8g}", "X-Axis-X-Max": f"{float(sm.t[-1]):.8g}",
        "X-Render-Info": (f"{info['source_shape'][0]}x{info['source_shape'][1]}"
                          f"->{info['rendered_shape'][0]}x{info['rendered_shape'][1]}"
                          f";{info['bytes']}"),
    }
    return Response(content=png, media_type="image/png", headers=headers)


@router.get("/{artifact_id}/curve")
def curve(
    artifact_id: str,
    kind: str = Query(..., pattern="^(integral|slope)$"),
    lam_min: float = 800.0,
    lam_max: float = 950.0,
    center: float = 950.0,
    half_width: float = 10.0,
) -> dict:
    """服务端版本的积分/斜率曲线。

    前端已经能用抽样谱实时算，这个端点是给"要全时间分辨率的最终结果"用的，
    以及作为前端计算的对照。两边算法相同（见 render.band_integral / wavelength_slope）。
    """
    sm = _load(artifact_id)
    if kind == "integral":
        y = render.band_integral(sm.M, sm.lam, lam_min, lam_max)
        label, unit = f"∫ {lam_min:g}–{lam_max:g} nm", "a.u.·nm"
    else:
        y = render.wavelength_slope(sm.M, sm.lam, center, half_width)
        label, unit = f"dI/dλ @ {center:g} nm", "a.u./nm"
    return {
        "kind": kind, "label": label, "unit": unit,
        "x": [round(float(v), 4) for v in sm.t],
        "y": [None if not np.isfinite(v) else round(float(v), 6) for v in y],
        "n_points": int(len(sm.t)),
    }


@router.get("/{artifact_id}/thickness")
def thickness(
    artifact_id: str,
    lam_min: float = Query(775.0, gt=0),
    lam_max: float = Query(1120.0, gt=0),
    max_points: int = Query(0, ge=0, le=5000),
) -> dict:
    """光学厚度 OT = n·d·cosθ vs 时间。

    算法是 fringe-optical-thickness 冻结规范的可执行副本
    （app/analysis/fringe_ot.py），STEP 0–10 逐条对应。

    默认窗口 775–1120 nm：775 避开约 775 nm 的吸收边，1120 是光谱仪上限。
    这是**平台传的 override**，规范里的 DEFAULTS 仍是 780–1050 —— 块 A
    回显的是本次实际用的值，所以两边都成立。
    """
    from app.analysis import fringe_ot

    if lam_min >= lam_max:
        raise ApiError(f"波段反了：{lam_min:g}–{lam_max:g} nm", 400, "bad_band")

    sm = _load(artifact_id)
    try:
        res = fringe_ot.extract_series(
            sm.lam, sm.t, sm.M,
            target_times_s="all",
            window_nm=[float(lam_min), float(lam_max)],
            input_is_absorbance=bool(sm.meta.get("input_is_absorbance", False)),
        )
    except fringe_ot.FringeError as exc:
        raise ApiError(str(exc), 400, "fringe_failed") from exc

    pts = res["points"]
    return {
        "x": [round(q["t"], 4) for q in pts],
        "y": [round(q["ot_nm"], 3) for q in pts],
        "flags": [q["flags"] for q in pts],
        "status": [q["status"] for q in pts],
        "cycles": [round(q["cycles"], 3) for q in pts],
        "snr_db": [round(q["snr_db"], 2) for q in pts],
        "label": f"OT @ {lam_min:g}–{lam_max:g} nm",
        "unit": "nm",
        "n_points": len(pts),
        "n_ok": res["n_ok"],
        "diagnostics": res["diagnostics"],
        # §5 要求块 A–D 全文，且禁止简化、禁止省略。这里原样带上，
        # 前端把它整段显示出来，不折叠。
        "report": fringe_ot.format_report(res, max_rows=max_points or 24),
    }


# ------------------------------------------------------------------ 样品清单
@router.get("/samples")
def samples() -> dict:
    """带光谱矩阵的样品清单 —— 「吸收光谱 & 膜厚」的入口列表。

    判断一个文件是不是光谱矩阵只读文件头（列数 ≥ 8），不整个解析。
    判断结果缓存进 artifact.meta_json，第二次就不用再看文件了。
    """
    import json as _json

    from app.storage import db

    rows = db.query(
        "SELECT a.artifact_id, a.filename, a.display_path, a.ext, a.size, a.status,"
        "       a.meta_json, a.is_matrix, a.sample_id, s.name AS sample_name, s.batch"
        " FROM artifact a JOIN sample s ON s.sample_id = a.sample_id"
        " WHERE a.kind='raw' AND a.ext IN ('.csv','.txt','.dat','.tsv','.asc',"
        "                                  '.xlsx','.xls','.xlsm')"
        " ORDER BY s.name, a.size DESC"
    )

    by_sample: dict[str, dict] = {}
    for r in rows:
        try:
            meta = _json.loads(r.get("meta_json") or "{}")
        except ValueError:
            meta = {}

        flag = r.get("is_matrix")
        if flag is None:
            flag = meta.get("matrix_like")
        if flag is None and r["status"] == "ok":
            flag = _probe_and_remember(r["artifact_id"], meta)

        entry = by_sample.setdefault(r["sample_id"], {
            "sample_id": r["sample_id"], "name": r["sample_name"],
            "batch": r["batch"], "matrices": [], "other_files": 0,
        })
        if flag:
            entry["matrices"].append({
                "artifact_id": r["artifact_id"], "filename": r["filename"],
                "display_path": r["display_path"], "size": r["size"],
                "status": r["status"], "columns_hint": meta.get("columns_hint"),
            })
        else:
            entry["other_files"] += 1

    out = [v for v in by_sample.values()]
    out.sort(key=lambda v: (not v["matrices"], v["name"]))
    return {
        "samples": out,
        "with_matrix": sum(1 for v in out if v["matrices"]),
        "total": len(out),
    }


def _probe_and_remember(artifact_id: str, meta: dict) -> bool:
    """只读文件头判断是不是矩阵，结果写回 artifact.meta_json。"""
    import json as _json

    from app.storage import db

    try:
        path = artifacts.local_path(artifact_id)
    except (KeyError, FileNotFoundError):
        return False

    try:
        if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
            flag, cols = True, None
        else:
            from app.parsers.sniff import sniff_text
            s = sniff_text(path, max_lines=40)   # 抬头可能有十几行，看少了会误判
            cols = len(s.columns)
            flag = bool(s.ok and cols >= 8)
    except Exception:
        flag, cols = False, None

    meta["matrix_like"] = flag
    if cols is not None:
        meta["columns_hint"] = cols
    with db.tx() as c:
        # is_matrix 是真列，筛选走它；meta_json 里那份留着给界面看细节
        c.execute("UPDATE artifact SET meta_json=?, is_matrix=? WHERE artifact_id=?",
                  (_json.dumps(meta, ensure_ascii=False), 1 if flag else 0, artifact_id))
    return flag
