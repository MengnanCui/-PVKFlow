"""模块的发现与热加载。

做法和 `app/skills/registry.py` 完全一样（有意的 —— 同事看哪一套都行）：

* 扫两个地方：`app/modules/builtin/*` 和 `workspace/modules/*`
* 一个模块 = 一个目录，里面有 `module.py`
* **单个模块加载失败只记错误，不拖垮服务** —— 同事放进来一个写错的模块，
  平台照常启动，设置页上那一条显示为「加载失败 + 原因」
* 加完点一下重载就生效，不用重启
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from app import config
from app.modules.base import Module, ModuleSpec
from app.skills.registry import load_module_from_path   # 同一个不撞车的导入器，不另写


@dataclass
class LoadError:
    source: str
    error: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"source": self.source, "error": self.error, "detail": self.detail}


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, Module] = {}
        self._errors: list[LoadError] = []
        self._dirs: dict[str, Path] = {}      # module_id → 它自己的目录

    def register(self, mod: Module, *, path: Path | None = None) -> None:
        spec = getattr(mod, "spec", None)
        if not isinstance(spec, ModuleSpec):
            raise TypeError(f"{type(mod).__name__} 缺少合法的 spec（要是 ModuleSpec 实例）")
        self._modules[spec.id] = mod
        if path is not None:
            self._dirs[spec.id] = path

    def get(self, module_id: str) -> Module:
        if module_id not in self._modules:
            raise KeyError(f"没有这个模块：{module_id}。"
                           f"已装的：{', '.join(sorted(self._modules)) or '（一个都没有）'}")
        return self._modules[module_id]

    def dir_of(self, module_id: str) -> Path | None:
        return self._dirs.get(module_id)

    def all(self) -> list[Module]:
        # order 小的排前面，同 order 再按名字。平台自带的排在同事的前面。
        return sorted(self._modules.values(),
                      key=lambda m: (m.spec.order, m.spec.name))

    def specs(self) -> list[dict]:
        return [m.spec.as_dict() for m in self.all()]

    @property
    def errors(self) -> list[dict]:
        return [e.as_dict() for e in self._errors]

    # -------------------------------------------------------------- 发现
    def load_all(self) -> None:
        self._modules.clear()
        self._errors.clear()
        self._dirs.clear()
        for root, origin in ((config.BUILTIN_MODULES_DIR, "builtin"),
                             (config.MODULES_DIR, "user")):
            if root.exists():
                self._load_dir(root, origin)

    def _load_dir(self, root: Path, origin: str) -> None:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith((".", "_")) or not entry.is_dir():
                continue
            f = entry / "module.py"
            if not f.is_file():
                self._errors.append(LoadError(
                    str(entry), "目录里没有 module.py",
                    "一个模块就是一个目录，里面至少要有 module.py。"
                    "照着 workspace/modules/_template/ 改最省事。"))
                continue
            try:
                self._load_one(f, origin, entry)
            except Exception as exc:
                # 这里必须吞掉 —— 一个坏模块不能让整个平台起不来
                self._errors.append(LoadError(
                    str(entry), f"{type(exc).__name__}: {exc}",
                    traceback.format_exc()[-2000:]))

    def _load_one(self, path: Path, origin: str, folder: Path) -> None:
        mod_py = load_module_from_path(path, f"hte_module_{folder.name}")

        found: list[Module] = []
        obj = getattr(mod_py, "MODULE", None)
        if isinstance(obj, Module):
            found.append(obj)
        else:
            for value in vars(mod_py).values():
                if (isinstance(value, type) and issubclass(value, Module)
                        and value is not Module and getattr(value, "spec", None)):
                    found.append(value())

        if not found:
            raise ValueError(
                "这个文件里没找到模块。要么给一个叫 MODULE 的实例"
                "（`MODULE = MyModule()`），要么定义一个带 spec 的 Module 子类。")

        for m in found:
            object.__setattr__(m.spec, "origin", origin)
            self.register(m, path=folder)


registry = ModuleRegistry()


def reload() -> dict:
    registry.load_all()
    return {"count": len(registry.all()), "modules": registry.specs(),
            "errors": registry.errors}


# 仓库里的模板目录。`_` 开头，扫描时会跳过，不会被当成一个模块加载。
TEMPLATE_DIR = config.ROOT / "app" / "modules" / "_template"


def seed_template() -> None:
    """把模板播一份到 workspace/modules/_template/。

    模板必须在仓库里（workspace/ 在 .gitignore 里，放那儿就发不出去了），
    但用户是在工作区里找它的 —— 「复制这个目录改一改」得真的有个目录可复制。
    已经存在就不动，免得覆盖掉别人改到一半的东西。
    """
    dest = config.MODULES_DIR / "_template"
    if dest.exists() or not TEMPLATE_DIR.is_dir():
        return
    import shutil
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, dest)
