"""跑一个 skill，并把结果落库。

这是「处理」与「存储」之间唯一的桥。所有结果都必须经过这里，
因为只有这里会写 analysis_run —— 没有 analysis_run 的结果是不可追溯的，
等于没有。
"""
from __future__ import annotations

import tempfile
import traceback
from pathlib import Path
from typing import Any, Sequence

from app import config
from app.skills.base import ChartSpec, FileRef, SkillContext, SkillResult
from app.skills.registry import registry
from app.storage import artifacts, db, results, tabular


def build_file_refs(artifact_ids: Sequence[str]) -> list[FileRef]:
    """把 artifact_id 解析成 skill 能直接用的 FileRef。

    复制型与引用型的差异在这里被吃掉——skill 永远只看到一个能打开的路径。
    """
    refs: list[FileRef] = []
    for aid in artifact_ids:
        row = artifacts.get(aid)
        if not row:
            raise KeyError(f"文件不存在于索引：{aid}")
        path = artifacts.local_path(aid)     # 文件丢了会在这里明确报错
        refs.append(FileRef(
            artifact_id=aid,
            path=path,
            filename=row["filename"],
            ext=(row["ext"] or "").lower(),
            size=row["size"] or 0,
            display_path=row["display_path"] or row["filename"],
            sample_id=row["sample_id"],
            sample_name=(db.scalar("SELECT name FROM sample WHERE sample_id=?", (row["sample_id"],))
                         or "") if row["sample_id"] else "",
            mime=row["mime"],
        ))
    return refs


def _coerce_params(spec, params: dict[str, Any]) -> dict[str, Any]:
    """按 ParamSpec 把前端传来的字符串转成正确类型，缺的补默认值。"""
    out = dict(spec.defaults())
    for p in spec.params:
        if p.key not in params or params[p.key] is None:
            continue
        v = params[p.key]
        try:
            if p.type == "number":
                out[p.key] = float(v) if not isinstance(v, bool) else v
            elif p.type == "bool":
                out[p.key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif p.type == "range" and isinstance(v, (list, tuple)):
                out[p.key] = [float(x) for x in v]
            else:
                out[p.key] = v
        except (TypeError, ValueError):
            out[p.key] = v      # 转不动就原样交给 skill，由它自己判断
    # skill 自己声明之外的参数也放行（高级用户可能直接调 API）
    for k, v in params.items():
        out.setdefault(k, v)
    return out


def run_skill(
    skill_id: str,
    artifact_ids: Sequence[str],
    params: dict[str, Any] | None = None,
    save: bool = True,
) -> dict:
    """执行一次处理。

    save=False 时只跑不写库——用于「先看看结果对不对」。
    save=True 时写 analysis_run + key_result + data_table + 派生图。
    """
    skill = registry.get(skill_id)
    spec = skill.spec

    if not spec.ready:
        raise RuntimeError(spec.ready_note or f"{spec.name} 尚未就绪")

    refs = build_file_refs(artifact_ids)
    if not refs:
        raise ValueError("没有选择输入文件")

    resolved = _coerce_params(spec, params or {})
    sample_id = next((r.sample_id for r in refs if r.sample_id), None)

    run_id = results.start_run(
        skill_id=spec.id, skill_version=spec.version, skill_name=spec.name,
        params=resolved, inputs=[r.artifact_id for r in refs], sample_id=sample_id,
        source="ai" if spec.origin == "skill.md" else "skill",
    ) if save else db.new_id("dry")

    config.ensure_dirs()
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as tmp:
        ctx = SkillContext(files=refs, params=resolved, run_id=run_id, tmp_dir=Path(tmp))
        try:
            result = skill.run(ctx)
        except Exception as exc:
            detail = traceback.format_exc()
            if save:
                results.finish_run(run_id, "failed", error=str(exc), log=detail)
            raise RuntimeError(f"{spec.name} 运行失败：{exc}") from exc

        if not isinstance(result, SkillResult):
            msg = f"{spec.name} 返回了 {type(result).__name__}，应该返回 SkillResult"
            if save:
                results.finish_run(run_id, "failed", error=msg)
            raise TypeError(msg)

        payload = _persist(run_id, spec, result, refs, sample_id, save=save)

    if save:
        results.finish_run(run_id, "ok", warnings=result.warnings,
                           log=result.logs or ctx.log_text)

    payload.update({
        "analysis_run_id": run_id if save else None,
        "saved": save,
        "skill": spec.as_dict(),
        "params": resolved,
        "summary": result.summary,
        "warnings": result.warnings,
        "inputs": [{"artifact_id": r.artifact_id, "filename": r.filename} for r in refs],
    })
    return payload


def _persist(run_id, spec, result: SkillResult, refs, sample_id, save: bool) -> dict:
    metric_dicts = [m.as_dict() for m in result.metrics]

    written = 0
    tables_meta: list[dict] = []
    figures_meta: list[dict] = []

    if save:
        written = results.write_results(
            run_id, metric_dicts, sample_id=sample_id,
            source="ai" if spec.origin == "skill.md" else "skill",
            version=spec.version,
            artifact_uri=refs[0].display_path if refs else None,
        )
        for name, frame in (result.tables or {}).items():
            try:
                tables_meta.append(tabular.write_table(run_id, name, frame))
            except Exception as exc:      # 单张表写失败不该毁掉整次运行
                result.warnings.append(f"数值表「{name}」保存失败：{exc}")
        for fig in result.figures:
            try:
                figures_meta.append(
                    artifacts.register_derived(run_id, fig.name, fig.data, fig.mime, sample_id)
                )
            except Exception as exc:
                result.warnings.append(f"图「{fig.name}」保存失败：{exc}")

    preview = result.preview.as_dict() if isinstance(result.preview, ChartSpec) else None
    return {
        "metrics": metric_dicts,
        "metrics_written": written,
        "tables": tables_meta,
        "figures": figures_meta,
        "preview": preview,
        "extra": result.extra,
    }


def run_detail(run_id: str) -> dict:
    run = db.query_one("SELECT * FROM analysis_run WHERE analysis_run_id = ?", (run_id,))
    if not run:
        raise KeyError(f"没有这次运行记录：{run_id}")
    return {
        "run": run,
        "results": results.results_for_run(run_id),
        "tables": tabular.tables_for_run(run_id),
        "figures": db.query(
            "SELECT artifact_id, filename, mime FROM artifact WHERE produced_by = ?", (run_id,)
        ),
    }


def recent_runs(limit: int = 50) -> list[dict]:
    return db.query(
        "SELECT r.*, s.name AS sample_name, s.batch AS sample_batch,"
        "       (SELECT COUNT(*) FROM key_result k WHERE k.analysis_run_id = r.analysis_run_id)"
        "         AS n_results"
        " FROM analysis_run r LEFT JOIN sample s ON s.sample_id = r.sample_id"
        " ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?", (limit,),
    )
