"""QFLS 成像接口（P-54）。处理走后台任务；结果全是输出目录里的文件，这里只读出来。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from app import tasks
from app.api.common import ApiError, guard
from app.qfls import pipeline, scan
from app.qfls import metrics as M
from app.qfls.task import KIND

router = APIRouter(prefix="/api/qfls", tags=["qfls"])
_SERVE = {".png": "image/png", ".csv": "text/csv; charset=utf-8", ".json": "application/json",
          ".md": "text/markdown; charset=utf-8"}



def _flag(v) -> bool:
    """JSON 里的 true / false；也认 "false" / "0" 这种字符串（原来字符串 "false" 被当成真，C069）。"""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


@router.get("/method")
def method() -> dict:
    """方法说明页要的：阈值、指标定义。和处理时用的是同一份 PARAMS。"""
    return {"params": M.PARAMS, "metrics": [{"key": k, "label": lab, "unit": u, "cross_objective": x}
                                            for k, lab, u, x in M.METRICS],
            "demo_dir": str(pipeline.demo_dir()), "runs_dir": str(pipeline.runs_dir())}


@router.post("/scan")
def preview(payload: dict = Body(...)) -> dict:
    """只扫描不处理：认出了哪些实验、多少帧、跳过了什么。"""
    root = guard(pipeline.check_root, payload.get("root", ""))
    sc = guard(scan.scan, root, [pipeline.runs_dir()])
    frames = sc.pop("frames")
    sc["n_frames"] = len(frames)
    sc["objectives"] = sorted({f["objective"] or "?" for f in frames})
    sc["total_bytes"] = sum(f["size_bytes"] for f in frames)
    sc["examples"] = [f["rel"] for f in frames[:5]]
    return sc


@router.get("/runs")
def runs() -> dict:
    return {"runs": pipeline.list_runs()}


@router.post("/runs")
def start(payload: dict = Body(...)) -> dict:
    is_demo = bool(payload.get("demo"))
    if not is_demo:
        guard(pipeline.check_root, payload.get("root", ""))
    params = payload.get("params") or {}
    guard(pipeline.merge_params, params)          # 键、类型、形状不对直接 400，不等跑完才发现每帧都出错
    run_id = pipeline.new_run_id()
    title = payload.get("title") or ("演示数据（假想）" if is_demo else Path(payload["root"]).name)
    task = tasks.submit(KIND, {"root": payload.get("root"), "demo": is_demo, "full": bool(payload.get("full")),
                               "run_id": run_id, "title": title, "figures": _flag(payload.get("figures", True)),
                               "params": params}, title=f"QFLS：{title}")
    return {"run_id": run_id, "task": task}


@router.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    run = guard(pipeline.load, run_id, "run.json")
    return {"run": run, "summary": pipeline.load(run_id, "summary.json"), "scan": pipeline.load(run_id, "scan.json")}


@router.get("/runs/{run_id}/points")
def points(run_id: str) -> dict:
    pts = guard(pipeline.load, run_id, "points.json")
    if pts is None:
        raise ApiError("这次处理还没完成（或失败了），没有样点表", 404, "not_ready")
    return {"points": pts}


@router.get("/runs/{run_id}/points/{key}")
def point(run_id: str, key: str) -> dict:
    return guard(pipeline.load_point, run_id, key)


@router.get("/runs/{run_id}/files/{path:path}")
def file(run_id: str, path: str):
    base = guard(pipeline.run_path, run_id).resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file() or target.suffix.lower() not in _SERVE:
        raise ApiError(f"没有这个文件：{path}", 404, "not_found")
    name = f"{run_id}_{path.replace('/', '_')}"
    return FileResponse(target, media_type=_SERVE[target.suffix.lower()],
                        headers={"Cache-Control": "no-store"} if target.suffix != ".png" else None,
                        filename=name if target.suffix.lower() in (".csv", ".md") else None)


@router.delete("/runs/{run_id}")
def delete(run_id: str) -> dict:
    run = guard(pipeline.load, run_id, "run.json")
    if run and run.get("status") == "running":
        raise ApiError("这次处理还在跑，先停掉再删", 409, "busy")
    guard(pipeline.delete_run, run_id)
    return {"ok": True}
