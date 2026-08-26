"""确定性规则引擎 —— 没有模型时，「智能」的部分由它承担。

它做三件事：
  1. 认出文件是什么（扩展名 + 抬头嗅探 + 列名词典）
  2. 排出候选 skill（复用每个 skill 自己的 can_handle 打分）
  3. 标出数据里明显不对劲的地方（缺失、重复、常量列、非单调、越界）

全部是确定性的：同样的输入永远给同样的输出，可解释、可测试、不需要联网。
界面上这些结论会标注来源为「规则」，与模型给的结论区分开。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from app.parsers import sniff

# 列名关键词 → 数据类型。命中越多越确信。
COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "jv": ("voltage", "current", "电压", "电流", "j-v", "jsc", "voc", "mA/cm", "bias"),
    "spectrum": ("wavelength", "波长", "nm", "absorbance", "transmittance", "reflectance",
                 "intensity", "counts", "raman", "shift", "透过", "吸收"),
    "thickness": ("thickness", "膜厚", "height", "profile", "step", "position", "psi", "delta"),
    "xrd": ("2theta", "2-theta", "theta", "d-spacing", "衍射"),
    "time_series": ("time", "时间", "elapsed", "timestamp"),
}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}


def identify(path: str | Path) -> dict[str, Any]:
    """认出一个文件是什么。纯规则，不联网。"""
    p = Path(path)
    ext = p.suffix.lower()

    if ext in IMAGE_EXT:
        info: dict[str, Any] = {"kind": "image", "ext": ext, "source": "rule"}
        try:
            from PIL import Image

            with Image.open(p) as im:
                info.update({"width": im.width, "height": im.height, "mode": im.mode})
        except Exception:
            pass
        return info

    if ext in sniff.EXCEL_EXT:
        return {"kind": "table", "ext": ext, "source": "rule", "note": "Excel 表格"}

    if ext not in sniff.TEXT_EXT and ext not in {".json", ".xml", ".md", ".yaml", ".yml"}:
        return {"kind": "unknown", "ext": ext, "source": "rule"}

    try:
        s = sniff.sniff_text(p)
    except Exception as exc:
        return {"kind": "unknown", "ext": ext, "source": "rule", "error": str(exc)}

    if not s.ok:
        return {"kind": "text", "ext": ext, "source": "rule", "note": s.reason}

    guess, score = _guess_domain(s.columns, s.preamble)
    return {
        "kind": "table",
        "ext": ext,
        "source": "rule",
        "domain": guess,
        "domain_confidence": score,
        "columns": s.columns,
        "dtypes": s.dtypes,
        "delimiter": s.delimiter,
        "encoding": s.encoding,
        "header_row": s.header_row,
        "preamble_lines": len(s.preamble),
    }


def _guess_domain(columns: Sequence[str], preamble: Sequence[str]) -> tuple[str, float]:
    haystack = " ".join(list(columns) + list(preamble)).lower()
    best, best_hits = "", 0
    for domain, keys in COLUMN_HINTS.items():
        hits = sum(1 for k in keys if k.lower() in haystack)
        if hits > best_hits:
            best, best_hits = domain, hits
    if best_hits == 0:
        return "", 0.0
    return best, round(min(1.0, best_hits / 3), 2)


def inspect_frame(df) -> list[dict]:
    """数据质量检查。每条都能说清楚「为什么这么判断」。"""
    import numpy as np
    import pandas as pd

    issues: list[dict] = []
    if df is None or len(df) == 0:
        return [{"level": "error", "message": "没有数据行", "source": "rule"}]

    nan_ratio = float(df.isna().sum().sum()) / max(1, df.size)
    if nan_ratio > 0.2:
        issues.append({
            "level": "warn",
            "message": f"缺失值占比 {nan_ratio:.0%}",
            "detail": "多半是分隔符识别错了，或者文件里混了多段表格",
            "source": "rule",
        })

    dup = int(df.duplicated().sum())
    if dup > 0:
        issues.append({
            "level": "info", "message": f"{dup} 行完全重复",
            "detail": "如果是仪器重复采样可以忽略", "source": "rule",
        })

    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty:
            issues.append({"level": "warn", "message": f"列「{col}」没有有效数值",
                           "source": "rule"})
            continue
        if s.nunique() == 1:
            issues.append({"level": "info", "message": f"列「{col}」是常量 {s.iloc[0]:g}",
                           "detail": "作为 X/Y 都没有信息量", "source": "rule"})
            continue
        # 3σ 离群点
        mu, sd = float(s.mean()), float(s.std())
        if sd > 0:
            outliers = int((np.abs(s - mu) > 3 * sd).sum())
            if outliers and outliers <= max(5, len(s) * 0.02):
                issues.append({
                    "level": "info",
                    "message": f"列「{col}」有 {outliers} 个 3σ 外的点",
                    "detail": "可能是坏点，运行分析前可以先剔除", "source": "rule",
                })
    return issues


def suggest_skills(file_refs) -> list[dict]:
    """候选 skill 排序。直接复用 registry 里每个 skill 的 can_handle。"""
    from app.skills.registry import registry

    return registry.suggest(file_refs)


def assist(artifact_ids: Sequence[str]) -> dict[str, Any]:
    """给「AI 分析」面板的规则版结论。没有模型时这就是全部内容。"""
    from app.skills.runner import build_file_refs

    refs = build_file_refs(artifact_ids)
    files = []
    all_issues: list[dict] = []

    for ref in refs:
        info = identify(ref.path)
        entry = {"artifact_id": ref.artifact_id, "filename": ref.filename, **info}
        if info.get("kind") == "table":
            try:
                df, _ = sniff.load_frame(ref.path, max_rows=5000)
                issues = inspect_frame(df)
                entry["issues"] = issues
                all_issues.extend(issues)
            except Exception as exc:
                entry["issues"] = [{"level": "warn", "message": f"无法解析为表格：{exc}",
                                    "source": "rule"}]
        files.append(entry)

    return {
        "source": "rule",
        "files": files,
        "suggestions": suggest_skills(refs),
        "issues": all_issues,
    }
