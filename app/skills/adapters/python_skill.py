"""Python 通道：把用户的 .py 变成注册好的 skill。

三种写法都支持，从简到繁：

1) 模块里放一个 SKILL 实例
       SKILL = MySkill()

2) 模块里定义 Skill 子类（自动实例化，可以有多个）
       class MySkill(Skill): ...

3) 模块里提供 register(registry)，完全自己控制
"""
from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

from app.skills.base import Skill, SkillSpec


def load_module(path: Path, origin: str = "user") -> list[Skill]:
    from app.skills import registry as reg

    module_name = f"hte_skill_{origin}_{path.parent.name}_{path.stem}".replace("-", "_")
    module = reg.load_module_from_path(path, module_name)

    found: list[Skill] = []

    inst = getattr(module, "SKILL", None)
    if inst is not None:
        found.append(inst)

    if not found:
        for _, obj in vars(module).items():
            if (inspect.isclass(obj) and issubclass(obj, Skill) and obj is not Skill
                    and isinstance(getattr(obj, "spec", None), SkillSpec)):
                found.append(obj())

    if not found:
        raise ValueError(
            f"{path} 里没有找到 skill。"
            f"请提供一个 SKILL 实例，或一个带 spec 的 Skill 子类。"
        )

    for s in found:
        if not isinstance(getattr(s, "spec", None), SkillSpec):
            raise TypeError(f"{path} 里的 {type(s).__name__} 缺少 spec")
        # 记录来源，界面上要区分「自带」和「你自己加的」
        s.spec = replace(s.spec, origin=s.spec.origin if origin == "builtin" else "user")
    return found
