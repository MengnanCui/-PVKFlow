"""只认本机、只认平台自己的页面（app/security.py）。

自查（2026-11-06，§6 #1）实测过：带外站 Origin、不带 Content-Type 的 POST
原来返回 200 并且真的执行了。浏览器里任何网页都能发这种请求。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(workspace):
    from app.main import app

    with TestClient(app) as c:
        yield c


EVIL = "https://evil.example"


def test_cross_site_simple_post_is_refused(client):
    """原样复现自查里那一条：外站 Origin + 不带 Content-Type。"""
    r = client.post("/api/selection/count", content=b'{"filter":{}}',
                    headers={"Origin": EVIL, "Content-Type": ""})
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["kind"] == "forbidden_origin"
    assert "evil.example" in err["message"]


def test_cross_site_settings_write_has_no_side_effect(client):
    before = client.get("/api/settings").json()
    r = client.post("/api/settings", json={"cache_limit_gb": 0.5},
                    headers={"Origin": EVIL})
    assert r.status_code == 403
    assert client.get("/api/settings").json() == before


def test_cross_site_module_install_never_runs_the_code(client, workspace):
    """装模块会执行 module.py。跨站请求连验证那一步都不该走到。"""
    marker = workspace / "pwned.txt"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("evil/module.py",
                   f"open({str(marker)!r}, 'w').write('x')\n")
    r = client.post("/api/modules/import",
                    files={"file": ("evil.zip", buf.getvalue(), "application/zip")},
                    headers={"Origin": EVIL})
    assert r.status_code == 403
    assert not marker.exists()


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_browser_marked_cross_site_is_refused_even_without_origin(client, site):
    r = client.post("/api/selection/count", json={"filter": {}},
                    headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403


def test_null_origin_is_refused(client):
    """file:// 页面和沙箱 iframe 发出来的 Origin 是字面量 null。"""
    r = client.post("/api/selection/count", json={"filter": {}},
                    headers={"Origin": "null"})
    assert r.status_code == 403


def test_dns_rebinding_host_is_refused_even_for_reads(client):
    """重绑定之后浏览器认为是同源，Origin 也对得上 —— 只有 Host 露馅。"""
    r = client.get("/api/files/roots", headers={"Host": "evil.example:8765"})
    assert r.status_code == 403
    r = client.get("/", headers={"Host": "evil.example:8765"})
    assert r.status_code == 403


def test_the_platforms_own_page_still_works(client):
    """同源的浏览器请求：Origin 和 Host 一致、Sec-Fetch-Site 是 same-origin。"""
    for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
        r = client.post("/api/selection/count", json={"filter": {}},
                        headers={"Host": host, "Origin": f"http://{host}",
                                 "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200, (host, r.text)


def test_scripts_and_cli_without_origin_still_work(client):
    """命令行 / 脚本不带 Origin，也不带 Sec-Fetch-*。"""
    r = client.post("/api/selection/count", json={"filter": {}},
                    headers={"Host": "127.0.0.1:8765"})
    assert r.status_code == 200


def test_another_local_port_is_not_the_same_origin(client):
    """本机另一个端口上的网页（比如某个开发服务器）也不是平台自己。"""
    r = client.post("/api/selection/count", json={"filter": {}},
                    headers={"Host": "127.0.0.1:8765",
                             "Origin": "http://127.0.0.1:3000"})
    assert r.status_code == 403


def test_following_a_link_into_the_app_is_fine(client):
    """从别的网站点链接打开平台首页是正常操作，不能拦。"""
    r = client.get("/", headers={"Host": "127.0.0.1:8765",
                                 "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_hostname_parsing():
    from app.security import _hostname

    assert _hostname("127.0.0.1:8765") == "127.0.0.1"
    assert _hostname("LOCALHOST") == "localhost"
    assert _hostname("[::1]:8765") == "::1"
    assert _hostname("evil.example:80") == "evil.example"
