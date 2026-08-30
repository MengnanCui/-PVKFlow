"""本地验证一个模块目录，不用打包上传。

    python -m app.modules.check workspace/modules/我的模块

为什么要有这个：你在自己机器上写模块，改一行就想知道对不对。
没有它的话循环是「改 → 打包 zip → 上传 → 看结果」，一轮小一分钟；
有了它一轮不到一秒。**那个循环快不快，直接决定你的模型能不能收敛。**

跑的是 `app/modules/validate.py` 那一份，和界面上「导入」时用的**完全同一份代码、
同一份文案** —— 两份文案迟早会漂，到时候你按命令行改完、导入还是不过，
那比没有这个命令还糟。`tests/test_module_cli.py` 断言两条路逐字相同。

退出码：通过 0，不通过 1。所以可以塞进你自己的脚本里：

    python -m app.modules.check 我的模块 && echo "可以导入了"
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_module_py(target: Path) -> Path | None:
    if target.is_file() and target.name == "module.py":
        return target
    if target.is_dir() and (target / "module.py").is_file():
        return target / "module.py"
    return None


def check(target: Path) -> tuple[bool, list[str]]:
    """返回 (通过没有, 要打印的行)。真正的检查在 validate.py 里。"""
    from app.modules import validate
    from app.modules.registry import ModuleRegistry

    lines: list[str] = []
    mod_py = _find_module_py(target)
    if mod_py is None:
        return False, [
            f"✗ {target} 里没有 module.py。",
            "  一个模块就是一个目录，里面至少要有 module.py。",
            "  照着 app/modules/_template/ 复制一份改最省事。",
        ]

    folder = mod_py.parent
    probe = ModuleRegistry()
    try:
        probe._load_one(mod_py, "user", folder)
    except Exception as exc:                        # noqa: BLE001
        return False, [f"✗ 加载失败：{type(exc).__name__}: {exc}"]

    if not probe.all():
        return False, [
            "✗ module.py 里没找到模块。",
            "  要么给一个叫 MODULE 的实例（`MODULE = MyModule()`），",
            "  要么定义一个带 spec 的 Module 子类。",
        ]

    mod = probe.all()[0]
    # 和已装的模块比 id 重名。装不上平台也没关系 —— 那就只查语法和形状。
    known: set[str] = set()
    try:
        from app.modules.registry import registry
        registry.load_all()
        known = {m.spec.id for m in registry.all()} - {mod.spec.id}
    except Exception:                               # noqa: BLE001
        pass

    r = validate.validate(mod, known_ids=known)

    lines.append(f"模块：{mod.spec.name}（{mod.spec.id} v{mod.spec.version}）")
    lines.append(f"查了：{' · '.join(r.checked)}")
    lines.append("")
    for e in r.errors:
        lines.append("✗ " + e.replace("\n", "\n  "))
    for w in r.warnings:
        lines.append("⚠ " + w.replace("\n", "\n  "))

    if r.ok and not r.warnings:
        lines.append("✓ 通过。可以放进 workspace/modules/ 了（然后在设置页点「重载」）。")
    elif r.ok:
        lines.append("")
        lines.append(f"✓ 通过，但有 {len(r.warnings)} 条提醒。")
    else:
        lines.append("")
        lines.append(f"{len(r.errors)} 条要改。改完再跑一次。")
        # 这句是给模型看的 —— 明确告诉人可以把上面整段贴回去
        lines.append("（把上面这段整个贴给你的模型，让它改。）")
    return r.ok, lines


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if args else 2

    ok_all = True
    for i, a in enumerate(args):
        if i:
            print()
        ok, lines = check(Path(a).expanduser())
        print("\n".join(lines))
        ok_all = ok_all and ok
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
