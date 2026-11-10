"""装 / 卸 / 导出功能模块（zip）。**先验证，通过了才落地**；验证不通过原样退回，每一条都说清楚。

原来写在路由文件 app/api/modules.py 里（C074 挪出来）：zip 解压、暂存、升级判定是模块的业务逻辑，
命令行、测试、以后别的入口都能直接调，不用经过 HTTP。错误抛 Invalid / NotFound（app/errors.py）。

分发靠 zip 收发，不靠 Git —— 同事之间用企业微信、U 盘、共享盘传都行。
"""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

from app import config
from app.errors import Invalid, NotFound
from app.modules import validate
from app.modules.registry import ModuleRegistry, registry, reload

# 一个模块的 zip 不该有多大。这道闸挡的是「不小心把整个数据目录打包进来了」。
MAX_ZIP_BYTES = 8 * 1024 * 1024
# 解开之后的总大小 / 文件数。压缩后很小、解开很大的 zip（zip 炸弹）只看上面那道闸是拦不住的。
# zip 里每个文件都声明了解压后多大，按声明先算 —— 声明是假的也没用：zipfile 读到声明的长度就停，
# 校验和对不上直接报错。
MAX_UNZIPPED_BYTES = 32 * 1024 * 1024
MAX_FILES = 500


def install_zip(blob: bytes, filename: str = "") -> dict:
    """装一个模块的 zip。

    - 同一个 id 已经装着（同事发来新版本）：**升级**，换掉的是那个模块自己的目录
    - 目录名撞上了另一个模块：换一个不撞车的目录名，**不动**那个模块
    - 目录名撞上的是一个装坏了的模块（加载失败）：原地换掉 —— 重新导入就是为了修它
    """
    if len(blob) > MAX_ZIP_BYTES:
        raise Invalid(f"这个 zip 超过 {MAX_ZIP_BYTES / 1e6:.0f} MB。模块里只该有代码，"
                       "不该带数据 —— 是不是把数据目录一起打包了？", "too_big")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise Invalid(f"这不是一个能打开的 zip：{exc}", "bad_zip") from exc

    infos = [i for i in zf.infolist() if not i.is_dir()]
    names = [i.filename for i in infos]
    if not names:
        raise Invalid("zip 是空的", "empty")
    unpacked = sum(i.file_size for i in infos)
    if unpacked > MAX_UNZIPPED_BYTES or len(infos) > MAX_FILES:
        raise Invalid(f"这个 zip 解开后有 {len(infos)} 个文件、{unpacked / 1e6:.1f} MB，"
                       f"超过 {MAX_FILES} 个 / {MAX_UNZIPPED_BYTES / 1e6:.0f} MB。"
                       "模块里只该有代码，不该带数据。", "too_big")

    # zip 里可能是 `module.py` 也可能是 `我的模块/module.py`，两种都认
    roots = {n.split("/", 1)[0] for n in names if "/" in n}
    top_level = [n for n in names if "/" not in n]
    if "module.py" in top_level:
        prefix, folder = "", Path(filename or "module").stem
    elif len(roots) == 1:
        folder = roots.pop()
        prefix = folder + "/"
        if f"{prefix}module.py" not in names:
            raise Invalid(f"zip 里的 {folder}/ 下面没有 module.py。"
                           "一个模块必须有这个文件。", "no_module_py")
    else:
        raise Invalid("zip 里有多个顶层目录，看不出哪个是模块。"
                       "请只打包一个模块的目录。", "ambiguous")

    folder = safe_name(folder)
    staging = config.TMP_DIR / f"module_import_{folder}"

    # 先解到暂存区验证，通过了才动真的目录 —— 验证不过不该留下半个模块
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for n in names:
            if prefix and not n.startswith(prefix):
                continue
            rel = n[len(prefix):]
            dest = (staging / rel).resolve()
            if staging.resolve() not in dest.parents and dest != staging.resolve():
                raise Invalid(f"zip 里有指向目录外的路径：{n}", "unsafe_path")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(n))

        report = validate_folder(staging)
        if not report["ok"]:
            return {"installed": False, "report": report,
                    "hint": "这几条改完再导入一次。每条都写了是哪个字段、"
                            "错在哪、合法值是什么。"}

        mid = report["module_id"]
        current = registry.get(mid) if mid in _ids() else None
        if current is not None:
            target = registry.dir_of(mid)          # 升级：换掉它自己的目录
            upgraded = {"from": current.spec.version, "to": report.get("version", "")}
        else:
            target = free_folder(folder)
            upgraded = None

        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staging), str(target))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    reload()
    return {"installed": True, "folder": target.name, "upgraded": upgraded,
            "report": report, "modules": registry.specs()}



def _ids() -> set[str]:
    return {m.spec.id for m in registry.all()}


def free_folder(name: str) -> Path:
    """装新模块用的目录：没人用、或者里面是一个装坏了的（没加载成功的）就用它；
    被另一个**正常加载**的模块占着，就换成 name_2、name_3 …… —— 绝不删别人的目录。"""
    taken = {registry.dir_of(i).resolve() for i in _ids() if registry.dir_of(i)}
    n, cand = 1, config.MODULES_DIR / name
    while cand.exists() and cand.resolve() in taken:
        n += 1
        cand = config.MODULES_DIR / f"{name}_{n}"
    return cand


def validate_folder(folder: Path) -> dict:
    """在暂存目录里加载并验证一个模块，不影响已装的那些。"""
    probe = ModuleRegistry()
    try:
        probe.load_file(folder / "module.py", "user", folder)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "module_id": "", "errors": [
            f"加载失败：{type(exc).__name__}: {exc}"], "warnings": [], "checked": []}

    if not probe.all():
        return {"ok": False, "module_id": "", "errors": [
            "module.py 里没找到模块。要么给一个叫 MODULE 的实例，"
            "要么定义一个带 spec 的 Module 子类。"], "warnings": [], "checked": []}

    mod = probe.all()[0]
    mid = mod.spec.id
    others = _ids()
    if mid in others:
        folder_of_current = registry.dir_of(mid)
        if folder_of_current and config.BUILTIN_MODULES_DIR in folder_of_current.parents:
            # 同 id 本来就是「升级」。但平台自带的不让同事的模块顶掉 —— 顶掉了，
            # 冻结的膜厚算法就被一个没人审过的版本替换了，而且下次更新又会被换回来
            return {"ok": False, "module_id": mid, "errors": [
                f"id \"{mid}\" 是平台自带的模块，不能被覆盖。给你的模块换一个 id。"],
                "warnings": [], "checked": []}
        others = others - {mid}                    # 同事模块的新版本：允许，按升级处理
    out = validate.validate(mod, known_ids=others).as_dict()
    out["version"] = mod.spec.version
    return out


def export_zip(module_id: str) -> tuple[bytes, str]:
    """打包成 zip 发给别人，返回 (内容, 文件名)。这就是「分发」的全部 —— 不需要 Git。"""
    folder = registry.dir_of(module_id)
    if not folder or not folder.is_dir():
        raise NotFound(f"找不到 {module_id} 的目录")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(folder.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                zf.write(f, f"{folder.name}/{f.relative_to(folder)}")
    return buf.getvalue(), f"{folder.name}.zip"


def uninstall(module_id: str) -> dict:
    folder = registry.dir_of(module_id)
    if not folder:
        raise NotFound(f"没有这个模块：{module_id}")
    # 平台自带的不让删 —— 删了就没有活示范了，而且下次更新还会回来
    if config.BUILTIN_MODULES_DIR in folder.parents:
        raise Invalid(f"「{module_id}」是平台自带的模块，不能卸载。", "builtin")
    shutil.rmtree(folder, ignore_errors=True)
    reload()
    return {"ok": True, "modules": registry.specs()}


def safe_name(name: str) -> str:
    """目录名只留安全字符 —— zip 是别人给的，不能拿它的字符串直接拼路径。"""
    keep = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in name)
    return (keep.strip("_") or "module")[:60]
