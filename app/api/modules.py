"""功能模块的接口：列表、重载、验证、面板计算、装卸。

分发靠 zip 收发，不靠 Git —— 同事之间用企业微信、U 盘、共享盘传都行。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response

from app import config
from app.api import bodies as B
from app.api.common import ApiError, guard
from app.modules import install, ops, validate
from app.modules.base import ModuleContext
from app.modules.registry import registry
from app.storage import artifacts

router = APIRouter(prefix="/api/modules", tags=["modules"])

@router.get("")
def list_modules() -> dict:
    return {
        "modules": registry.specs(),
        "errors": registry.errors,        # 装失败的也要显示出来，不能静默消失
        "ops": ops.catalog(),
        "modules_dir": str(config.MODULES_DIR),
    }


@router.post("/reload")
def reload_modules() -> dict:
    from app.modules.registry import reload
    return reload()


@router.post("/validate")
def validate_module(body: B.ModuleValidateBody | None = None) -> dict:
    """验证一个已装的模块。装之前的验证走 /import。"""
    payload = B.dump(body)
    mid = (payload.get("module_id") or "").strip()
    mod = guard(registry.get, mid)
    others = {m.spec.id for m in registry.all()} - {mid}
    return validate.validate(mod, known_ids=others).as_dict()


@router.post("/{module_id}/compute")
def compute(module_id: str, body: B.ModuleComputeBody) -> dict:
    """拿全分辨率矩阵算一遍这个模块的所有面板。

    A 档面板在浏览器里已经用抽样谱算过一次了（那是预览）；这里是精确值。
    B 档面板只有这一条路。
    """
    payload = B.dump(body)
    mod = guard(registry.get, module_id)

    artifact_id = (payload.get("artifact_id") or "").strip()
    if not artifact_id:
        raise ApiError("要指定 artifact_id", 400)

    sm = guard(artifacts.load_matrix, artifact_id)
    params = {**mod.spec.defaults(), **(payload.get("params") or {})}

    # 界面说这次动了哪几个控件。据此算出「哪些面板的结果可能变了」——
    # `uses=[]` 的面板（不依赖任何控件）永远不变，没必要陪着重算一遍。
    changed = payload.get("changed")
    ctx = ModuleContext(sm.lam, sm.M, sm.t, params,
                        meta=sm.meta, artifact_id=artifact_id)
    if isinstance(changed, list):
        ch = set(changed)
        ctx.only(p.id for p in mod.spec.panels if p.uses and (set(p.uses) & ch))

    try:
        out = mod.compute(ctx)
    except Exception as exc:                        # noqa: BLE001
        # 模块是同事写的，崩了要说清是**哪个模块**崩的，
        # 不能让它看起来像平台的毛病
        raise ApiError(f"模块「{mod.spec.name}」算不出来：{type(exc).__name__}: {exc}",
                       400, "module_failed") from exc

    return {
        "module_id": module_id,
        "panels": {pid: d.as_dict() for pid, d in out.items()},
        "n_points": int(len(sm.t)),
        "params": params,
    }


# ---------------------------------------------------------------- 装 / 卸 / 传（逻辑在 app/modules/install.py）
@router.post("/import")
def import_module(file: UploadFile = File(...)) -> dict:
    """装一个模块的 zip。**先验证，通过了才落地**；同 id 是升级，平台自带的不让覆盖。

    写成同步函数：FastAPI 会放到线程池里跑。解压、验证（要真的跑一遍模块）、重载都是阻塞的活（自查 §6 #26）。
    """
    blob = file.file.read(install.MAX_ZIP_BYTES + 1)
    return guard(install.install_zip, blob, file.filename or "")


@router.get("/{module_id}/export")
def export_module(module_id: str) -> Response:
    """打包成 zip 发给别人。这就是「分发」的全部 —— 不需要 Git。"""
    blob, name = guard(install.export_zip, module_id)
    return Response(blob, media_type="application/zip", headers={"Content-Disposition": _attachment(name)})


def _attachment(name: str) -> str:
    """下载文件名。响应头只能是 latin-1：中文名原样塞进去就是 500（自查 §6 #15）。
    按 RFC 5987 给 UTF-8 的 `filename*`（现在的浏览器都认），再给一个 ASCII 的兜底名。"""
    ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_.") else "_" for ch in name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


@router.delete("/{module_id}")
def uninstall(module_id: str) -> dict:
    """卸载（删它的目录、重载）。平台自带的不让卸。"""
    return guard(install.uninstall, module_id)
