"""Skill 的发现、注册与推荐。

扫描两个地方：
  * app/skills/builtin/*      —— 平台自带
  * workspace/skills/*        —— 用户拖进来的，加完点一下「重新加载」就生效，不用重启

每个 skill 是一个目录，里面有 `skill.py`（Python 通道）或 `SKILL.md`（模型通道）。
单个 skill 加载失败只记录错误，不拖垮整个服务——这一点很重要，
否则用户加一个写错的 skill 会让整个平台起不来。
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app import config
from app.skills.base import FileRef, Skill, SkillSpec


@dataclass
class LoadError:
    source: str
    error: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"source": self.source, "error": self.error, "detail": self.detail}


class Registry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._errors: list[LoadError] = []

    # -------------------------------------------------------------- 注册
    def register(self, skill: Skill, *, replace: bool = True) -> None:
        spec = getattr(skill, "spec", None)
        if not isinstance(spec, SkillSpec):
            raise TypeError(f"{type(skill).__name__} 缺少合法的 spec")
        if spec.id in self._skills and not replace:
            raise ValueError(f"skill id 重复：{spec.id}")
        self._skills[spec.id] = skill

    def get(self, skill_id: str) -> Skill:
        if skill_id not in self._skills:
            raise KeyError(f"没有这个 skill：{skill_id}")
        return self._skills[skill_id]

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: (s.spec.category, s.spec.name))

    def specs(self) -> list[dict]:
        return [s.spec.as_dict() for s in self.all()]

    @property
    def errors(self) -> list[dict]:
        return [e.as_dict() for e in self._errors]

    def categories(self) -> list[dict]:
        buckets: dict[str, int] = {}
        for s in self._skills.values():
            buckets[s.spec.category] = buckets.get(s.spec.category, 0) + 1
        return [{"category": k, "count": v} for k, v in sorted(buckets.items())]

    # -------------------------------------------------------------- 发现
    def load_all(self) -> None:
        self._skills.clear()
        self._errors.clear()
        for root, origin in ((config.BUILTIN_SKILLS_DIR, "builtin"), (config.SKILLS_DIR, "user")):
            if root.exists():
                self._load_dir(root, origin)

    def _load_dir(self, root: Path, origin: str) -> None:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith((".", "_")):
                continue
            try:
                if entry.is_dir():
                    if (entry / "skill.py").is_file():
                        self._load_python(entry / "skill.py", origin)
                    elif (entry / "SKILL.md").is_file():
                        self._load_skill_md(entry / "SKILL.md", origin)
                    else:
                        self._errors.append(LoadError(
                            str(entry), "目录里既没有 skill.py 也没有 SKILL.md"))
                elif entry.suffix == ".py":
                    self._load_python(entry, origin)
                elif entry.name.upper() == "SKILL.MD":
                    self._load_skill_md(entry, origin)
            except Exception as exc:
                self._errors.append(LoadError(str(entry), str(exc), traceback.format_exc()[-2000:]))

    def _load_python(self, path: Path, origin: str) -> None:
        """加载一个 Python skill 模块。

        模块里满足下面任一即可：
          * 一个名为 SKILL 的 Skill 实例
          * 一个或多个 Skill 子类（会各自实例化）
          * 一个 register(registry) 函数（完全自定义）
        """
        from app.skills.adapters import python_skill

        for skill in python_skill.load_module(path, origin):
            self.register(skill)

    def _load_skill_md(self, path: Path, origin: str) -> None:
        from app.skills.adapters import skillmd

        self.register(skillmd.load(path, origin))

    # -------------------------------------------------------------- 推荐
    def suggest(self, files: Sequence[FileRef], limit: int = 6) -> list[dict]:
        """给一组文件排出候选 skill。纯确定性打分，不需要模型。"""
        scored = []
        for skill in self._skills.values():
            try:
                score = float(skill.can_handle(files))
            except Exception:
                score = 0.0
            if score > 0:
                scored.append({
                    "skill_id": skill.spec.id,
                    "name": skill.spec.name,
                    "category": skill.spec.category,
                    "ready": skill.spec.ready,
                    "score": round(score, 3),
                })
        scored.sort(key=lambda r: (-r["score"], r["name"]))
        return scored[:limit]


# 进程内单例
registry = Registry()


def _ensure_import_path() -> None:
    root = str(config.ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def load_module_from_path(path: Path, module_name: str):
    """从任意路径导入一个模块，用一个不会撞车的模块名。"""
    _ensure_import_path()
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def reload() -> dict:
    registry.load_all()
    return {
        "count": len(registry.all()),
        "skills": registry.specs(),
        "errors": registry.errors,
    }
