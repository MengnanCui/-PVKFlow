"""Skill 的列表、推荐、运行。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query

from app.api.common import ApiError, guard
from app.skills import runner
from app.skills.registry import registry, reload as reload_registry

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
def list_skills() -> dict:
    return {
        "skills": registry.specs(),
        "categories": registry.categories(),
        "errors": registry.errors,
    }


@router.post("/reload")
def reload_skills() -> dict:
    """加了新 skill 之后点一下，不用重启服务。"""
    return reload_registry()


@router.get("/{skill_id}")
def get_skill(skill_id: str) -> dict:
    try:
        return registry.get(skill_id).spec.as_dict()
    except KeyError as exc:
        raise ApiError(str(exc).strip("'\""), 404, "not_found") from exc


@router.post("/suggest")
def suggest(payload: dict = Body(...)) -> dict:
    """给一组文件排候选 skill。确定性打分，不需要模型。"""
    ids = payload.get("artifact_ids") or []
    if not ids:
        return {"suggestions": [], "source": "rule"}
    refs = guard(runner.build_file_refs, ids)
    return {"suggestions": registry.suggest(refs), "source": "rule"}


@router.post("/run")
def run(payload: dict = Body(...)) -> dict:
    """执行一次处理。

    save=false 时只跑不写库，用来先看看结果对不对。
    """
    skill_id = payload.get("skill_id")
    ids = payload.get("artifact_ids") or []
    if not skill_id:
        raise ApiError("缺少 skill_id", 400)
    if not ids:
        raise ApiError("请先选择要处理的文件", 400)
    return guard(runner.run_skill, skill_id, ids, payload.get("params") or {},
                 bool(payload.get("save", True)))


@router.get("/runs/recent")
def recent(limit: int = Query(50, le=500)) -> dict:
    return {"runs": runner.recent_runs(limit)}
