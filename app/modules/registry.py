"""模块的发现与热加载。

怎么找、怎么装：

* 扫两个地方：`app/modules/builtin/*` 和 `workspace/modules/*`
* 一个模块 = 一个目录，里面有 `module.py`
* **单个模块加载失败只记错误，不拖垮服务** —— 同事放进来一个写错的模块，
  平台照常启动，设置页上那一条显示为「加载失败 + 原因」
* 加完点一下重载就生效，不用重启
"""
from __future__ import annotations

import importlib.util
import sys

import traceback
from dataclasses import dataclass
from pathlib import Path

from app import config
from app.modules.base import Module, ModuleSpec


@dataclass
class LoadError:
    source: str
    error: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"source": self.source, "error": self.error, "detail": self.detail}


def load_module_from_path(path: Path, module_name: str):
    """从任意路径导入一个模块，用一个不会撞车的模块名。

    同事的 module.py 放在工作区里，不在 Python 的导入路径上；
    它里面写的 `from app.modules.base import ...` 要能找到平台的包，
    所以先把项目根目录放进 sys.path。
    """
    root = str(config.ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
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


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, Module] = {}
        self._errors: list[LoadError] = []
        self._dirs: dict[str, Path] = {}      # module_id → 它自己的目录

    def register(self, mod: Module, *, path: Path | None = None,
                 _into: tuple[dict, dict] | None = None) -> None:
        spec = getattr(mod, "spec", None)
        if not isinstance(spec, ModuleSpec):
            raise TypeError(f"{type(mod).__name__} 缺少合法的 spec（要是 ModuleSpec 实例）")
        modules, dirs = _into if _into is not None else (self._modules, self._dirs)
        if spec.id in modules:
            # 同 id 的第二个**不许静默顶替**第一个。内置模块先装，所以同事的模块
            # 撞了内置的 id 时，内置的留着、这一个装不上并说清原因 ——
            # 原来是后来的直接覆盖，平台自己的膜厚模块可能被悄悄换掉（自查 §6 #12）。
            where = dirs.get(spec.id)
            raise ValueError(f"模块 id「{spec.id}」已经被占用了"
                             f"（{where.name if where else '另一个模块'}）。换一个 id 再装")
        modules[spec.id] = mod
        if path is not None:
            dirs[spec.id] = path

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
        """重新发现全部模块。**先装到新的容器里，最后一次性换上。**

        原来是先 clear() 再一个个装：批处理的计算线程这时候去读，
        读到的是空的或装了一半的 —— 曲线整列变 NaN、样品还标成功，
        或者直接 "dict changed size during iteration"（自查 §6 #12）。
        现在读的人拿到的要么是旧的一整套，要么是新的一整套。
        """
        modules: dict[str, Module] = {}
        errors: list[LoadError] = []
        dirs: dict[str, Path] = {}
        for root, origin in ((config.BUILTIN_MODULES_DIR, "builtin"),
                             (config.MODULES_DIR, "user")):
            if root.exists():
                self._load_dir(root, origin, (modules, dirs), errors)
        self._modules, self._errors, self._dirs = modules, errors, dirs

    def _load_dir(self, root: Path, origin: str, into: tuple[dict, dict],
                  errors: list) -> None:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith((".", "_")) or not entry.is_dir():
                continue
            f = entry / "module.py"
            if not f.is_file():
                errors.append(LoadError(
                    str(entry), "目录里没有 module.py",
                    "一个模块就是一个目录，里面至少要有 module.py。"
                    "照着 workspace/modules/_template/ 改最省事。"))
                continue
            try:
                self._load_one(f, origin, entry, into)
            except Exception as exc:
                # 这里必须吞掉 —— 一个坏模块不能让整个平台起不来
                errors.append(LoadError(
                    str(entry), f"{type(exc).__name__}: {exc}",
                    traceback.format_exc()[-2000:]))

    def _load_one(self, path: Path, origin: str, folder: Path,
                  into: tuple[dict, dict] | None = None) -> None:
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
            self.register(m, path=folder, _into=into)


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
