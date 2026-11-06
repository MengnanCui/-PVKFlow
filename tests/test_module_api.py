"""/api/modules/* —— 装、卸、导出、计算。

自查 §6.3 列出的测试缺口：这一组接口原来一条测试都没有，
包括 zip 里「指向目录外的路径」那一条安全分支。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

GOOD = '''
from app.modules.base import Control, Curve, Module, ModuleSpec, Op, Panel

class Red(Module):
    spec = ModuleSpec(
        id="colleague.red_band", name="红光积分", version="0.3.0",
        controls=[Control("band", "波段", "band", default=[900, 1000], unit="nm")],
        panels=[Panel("red", "红光积分 vs 时间", uses=["band"],
                      y_label="红光积分 (a.u.·nm)", live=Op.band_integral(band="band"))],
        batch_curves=[Curve("red_integral", from_panel="red")],
    )

MODULE = Red()
'''

CRASHES = '''
from app.modules.base import Module, ModuleSpec, Panel

class Boom(Module):
    spec = ModuleSpec(id="colleague.boom", name="会崩的模块", version="0.1.0",
                      panels=[Panel("p", "崩")])

    def compute(self, ctx):
        raise RuntimeError("同事的算法里有个 bug")

MODULE = Boom()
'''


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


@pytest.fixture()
def client(workspace):
    from app.main import app
    from app.modules.registry import reload

    reload()
    with TestClient(app) as c:
        yield c


def _install(client, blob: bytes, filename: str = "m.zip"):
    return client.post("/api/modules/import",
                       files={"file": (filename, blob, "application/zip")})


def test_install_list_export_uninstall_round_trip(client, workspace):
    r = _install(client, _zip({"red_band/module.py": GOOD}))
    assert r.status_code == 200 and r.json()["installed"], r.text
    ids = {m["id"] for m in client.get("/api/modules").json()["modules"]}
    assert "colleague.red_band" in ids

    z = zipfile.ZipFile(io.BytesIO(client.get("/api/modules/colleague.red_band/export").content))
    assert "red_band/module.py" in z.namelist()

    assert client.delete("/api/modules/colleague.red_band").json()["ok"]
    ids = {m["id"] for m in client.get("/api/modules").json()["modules"]}
    assert "colleague.red_band" not in ids
    assert not (workspace / "modules" / "red_band").exists()


def test_a_bare_module_py_at_the_zip_root_is_accepted(client, workspace):
    r = _install(client, _zip({"module.py": GOOD}), filename="red_band.zip")
    assert r.json()["installed"], r.text
    assert (workspace / "modules" / "red_band" / "module.py").is_file()


def test_a_module_that_fails_validation_leaves_nothing_behind(client, workspace):
    bad = GOOD.replace('uses=["band"]', 'uses=["bnad"]')       # 拼错的控件名
    r = _install(client, _zip({"red_band/module.py": bad}))
    body = r.json()
    assert r.status_code == 200 and body["installed"] is False
    assert any("bnad" in e for e in body["report"]["errors"])
    assert not (workspace / "modules" / "red_band").exists()
    assert not list((workspace / "tmp").glob("module_import_*"))   # 暂存区也清干净了


def test_paths_escaping_the_module_folder_are_refused(client, workspace):
    """zip 是别人给的：`../` 不能把文件写到模块目录外面。"""
    r = _install(client, _zip({"evil/module.py": GOOD,
                               "evil/../../escaped.py": "print('x')"}))
    assert r.status_code == 400 and r.json()["error"]["kind"] == "unsafe_path"
    assert not list(workspace.rglob("escaped.py"))
    assert not (workspace / "modules" / "evil").exists()


@pytest.mark.parametrize("blob, kind", [
    (b"not a zip at all", "bad_zip"),
    (_zip({}), "empty"),
    (_zip({"a/module.py": GOOD, "b/module.py": GOOD}), "ambiguous"),
    (_zip({"a/readme.txt": "没有 module.py"}), "no_module_py"),
])
def test_malformed_zips_say_what_is_wrong(client, blob, kind):
    r = _install(client, blob)
    assert r.status_code == 400 and r.json()["error"]["kind"] == kind


def test_an_oversized_zip_is_refused_before_unpacking(client, monkeypatch):
    from app.api import modules as api_modules

    monkeypatch.setattr(api_modules, "MAX_ZIP_BYTES", 100)
    r = _install(client, _zip({"red_band/module.py": GOOD}))
    assert r.status_code == 400 and r.json()["error"]["kind"] == "too_big"


def test_builtin_modules_cannot_be_uninstalled(client):
    r = client.delete("/api/modules/thickness.fringe_ot")
    assert r.status_code == 400 and r.json()["error"]["kind"] == "builtin"


# ---------------------------------------------------------------- 自查 §6 #15
def _versions(client) -> dict[str, str]:
    return {m["id"]: m["version"] for m in client.get("/api/modules").json()["modules"]}


def test_a_module_in_a_chinese_folder_can_be_exported(client):
    """文档示例鼓励起中文名。原来导出时文件名直接塞进响应头，非 ASCII 就是 500。"""
    assert _install(client, _zip({"红光积分/module.py": GOOD})).json()["installed"]
    r = client.get("/api/modules/colleague.red_band/export")
    assert r.status_code == 200, r.text
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''%E7%BA%A2%E5%85%89%E7%A7%AF%E5%88%86.zip" in cd
    assert 'filename="' in cd                      # 老浏览器的 ASCII 兜底名也在
    assert "红光积分/module.py" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()


def test_a_different_module_in_a_same_named_folder_does_not_replace_the_first(client, workspace):
    """目录名一样、id 不一样 = 两个不同的模块。原来后装的会把先装的整个目录删掉。"""
    other = GOOD.replace("colleague.red_band", "colleague.blue_band").replace("红光", "蓝光")
    assert _install(client, _zip({"band/module.py": GOOD})).json()["installed"]
    r = _install(client, _zip({"band/module.py": other})).json()
    assert r["installed"], r
    assert {"colleague.red_band", "colleague.blue_band"} <= set(_versions(client))
    assert r["folder"] != "band"                   # 换了个不撞车的目录名
    assert (workspace / "modules" / "band" / "module.py").read_text(encoding="utf-8") == GOOD


def test_importing_the_same_id_again_upgrades_it(client, workspace):
    """同事发来新版本：原来报「id 重名」，只能先卸载再装。现在是升级，换掉的是它自己的目录。"""
    assert _install(client, _zip({"red_band/module.py": GOOD})).json()["installed"]
    newer = GOOD.replace('version="0.3.0"', 'version="0.4.0"')
    r = _install(client, _zip({"red_band_v2/module.py": newer})).json()
    assert r["installed"] and r["upgraded"] == {"from": "0.3.0", "to": "0.4.0"}, r
    assert _versions(client)["colleague.red_band"] == "0.4.0"
    assert r["folder"] == "red_band"               # 装回原来的目录，不多出一份
    assert not (workspace / "modules" / "red_band_v2").exists()


def test_a_module_cannot_take_over_a_builtin_id(client):
    hijack = GOOD.replace("colleague.red_band", "thickness.fringe_ot")
    r = _install(client, _zip({"x/module.py": hijack})).json()
    assert r["installed"] is False
    assert any("平台自带" in e for e in r["report"]["errors"]), r["report"]


def test_reimporting_fixes_a_broken_module_in_the_same_folder(client, workspace):
    """目录里是一个装坏了（加载失败）的模块：重新导入就是为了修它，应该原地换掉。"""
    from app.modules.registry import reload

    broken = workspace / "modules" / "red_band"
    broken.mkdir(parents=True)
    (broken / "module.py").write_text("this is not python", encoding="utf-8")
    reload()
    r = _install(client, _zip({"red_band/module.py": GOOD})).json()
    assert r["installed"] and r["folder"] == "red_band", r
    assert client.get("/api/modules").json()["errors"] == []


def test_the_unpacked_size_is_limited_too(client, monkeypatch, workspace):
    """压缩后很小、解开很大（zip 炸弹）：按 zip 里声明的解压后大小先拦住，一个字节都不写。"""
    from app.api import modules as api_modules

    monkeypatch.setattr(api_modules, "MAX_UNZIPPED_BYTES", 10_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("red_band/module.py", GOOD)
        z.writestr("red_band/padding.txt", "0" * 50_000)
    blob = buf.getvalue()
    assert len(blob) < 10_000
    r = _install(client, blob)
    assert r.status_code == 400 and r.json()["error"]["kind"] == "too_big", r.text
    assert not (workspace / "modules" / "red_band").exists()


# ---------------------------------------------------------------- 计算
@pytest.fixture()
def matrix_id(client, tmp_path):
    from app.storage import db, ingest
    from tests.test_insitu_csv import write_data_csv

    d = tmp_path / "ZG0099_2026072918354709_Mode5_202607291932_SPS100"
    d.mkdir()
    write_data_csv(d / "Data.csv", n_lam=200, ot=5000.0)
    ingest.ingest_paths(ingest.scan_folders(tmp_path)["files"])
    return db.scalar("SELECT artifact_id FROM artifact WHERE filename='Data.csv'")


def test_compute_returns_every_panel_of_the_module(client, matrix_id):
    r = client.post("/api/modules/special.slope_integral/compute",
                    json={"artifact_id": matrix_id, "params": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["panels"]) == {"slope", "integ"}
    assert len(body["panels"]["integ"]["y"]) == body["n_points"]


def test_compute_names_the_module_that_crashed(client, matrix_id):
    """模块是同事写的，崩了要说清是哪个模块，不能看起来像平台的毛病。"""
    from app.modules.registry import registry

    ns: dict = {}
    exec(CRASHES, ns)
    # 直接塞进已装列表（装的那条路要过验证，而验证会先试跑、当场就发现它会崩）。
    # 下一条测试的 client 夹具会 reload()，不会带过去。
    registry._modules = {**registry._modules, "colleague.boom": ns["MODULE"]}
    r = client.post("/api/modules/colleague.boom/compute", json={"artifact_id": matrix_id})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["kind"] == "module_failed" and "会崩的模块" in err["message"]


def test_compute_on_an_unknown_module_is_a_404(client, matrix_id):
    r = client.post("/api/modules/nope.nope/compute", json={"artifact_id": matrix_id})
    assert r.status_code == 404
