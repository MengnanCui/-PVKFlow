"""QFLS 成像（P-54）：扫描、校验、指标、评分、输出目录、接口。

数据全是 app/qfls/demo.py 生成的假想数据（矩阵缩到 48 × 72，测试才快）。
"""
import codecs
import csv
import hashlib
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from app.qfls import demo, frame, metrics, names, pipeline, scan

TINY = (48, 72)


@pytest.fixture()
def data(tmp_path):
    root = tmp_path / "QFLS数据"
    demo.generate(root, shape=TINY)
    return root


def _tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest() + str(p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------ 名字
def test_mojibake_names_are_fixed_for_display_only():
    gbk_as_cp437 = "钝化剂筛选".encode("gbk").decode("cp437")
    assert names.fix_name(gbk_as_cp437) == ("钝化剂筛选", "GBK 被当成 cp437 解了")
    utf8_as_gbk = "样品".encode("utf-8").decode("gbk")           # 「鏍峰搧」
    assert names.fix_name(utf8_as_gbk) == ("样品", "UTF-8 被当成 GBK 解了")
    assert names.fix_name("退火温度对比") == ("退火温度对比", None)       # 本来就对的不动
    assert names.fix_name("slot1") == ("slot1", None)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上文件名本来就是 Unicode，不会有 surrogate")
def test_gbk_bytes_on_disk_are_read_as_chinese(tmp_path):
    raw = os.fsdecode("钝化剂".encode("gbk"))
    assert names.fix_name(raw) == ("钝化剂", "原始字节按 GBK 解")


def test_frame_names():
    f = names.parse_frame_name("0-20X-405R-1sun-20261102093015-7d8aa9fd-0d38-0170-2c22-3ad16b19d293.txt")
    assert (f["objective"], f["laser"], f["intensity_sun"], f["timestamp"]) == ("20X", "405R", 1.0, "2026-11-02T09:30:15")
    assert f["uuid"] == "7d8aa9fd-0d38-0170-2c22-3ad16b19d293" and not f["problems"]
    g = names.parse_frame_name("0-100X-405R-1sun-1762075815000-" + "a" * 32 + ".txt")
    assert g["objective"] == "100X" and g["timestamp"] and not g["problems"]
    bad = names.parse_frame_name("0-whatever.txt")
    assert bad["objective"] is None and bad["problems"]
    assert names.is_frame_file("0-x.txt") and not names.is_frame_file("1-20X.txt")
    assert not names.is_frame_file("0-20X-heatmap.png")


# ------------------------------------------------------------------ 扫描
def test_scan_recognises_levels_and_skips_the_rest(data):
    sc = scan.scan(data)
    assert len(sc["frames"]) == 60
    exps = {e["name"]: e for e in sc["experiments"]}
    assert set(exps) == {"退火温度对比", "钝化剂筛选", "substrate_ITO_vs_FTO"}
    assert len(exps["钝化剂筛选"]["batches"]) == 2 and exps["钝化剂筛选"]["n_frames"] == 24
    skipped = {s["path"]: s["reason"] for s in sc["skipped"]}
    for p in ("备份说明", "PL光谱_2026", "_tmp", "说明.txt", "退火温度对比/a3f9c2e17b/备注.md",
              "退火温度对比/a3f9c2e17b/slot2/thumbs"):
        assert p in skipped, p
    assert sc["renamed"][0]["fixed"] == "钝化剂筛选"
    assert sc["artifacts"] > 10
    assert all(f["file"].startswith("0") and f["file"].endswith(".txt") for f in sc["frames"])
    assert {f["objective"] for f in sc["frames"]} == {"20X", "100X"}
    assert len({f["key"] for f in sc["frames"]}) == 60


def test_scan_does_not_assume_counts(tmp_path):
    root = tmp_path / "r"
    q = root / "exp" / "abc123" / "slot7" / "point12" / "QFLS"
    q.mkdir(parents=True)
    demo.write_txt(q / "0-20X-405R-1sun-20261101120000-0123456789abcdef0123456789abcdef.txt", np.full((8, 9), 1.2))
    sc = scan.scan(root)
    assert [(f["slot"], f["point"]) for f in sc["frames"]] == [("slot7", "point12")]


# ------------------------------------------------------------------ 读帧 / 校验
def test_read_matrix_checks_shape_missing_and_range(tmp_path):
    good = tmp_path / "g.txt"
    demo.write_txt(good, np.full((6, 7), 1.2))
    assert good.read_bytes().startswith(codecs.BOM_UTF8)
    arr, info = frame.read_matrix(good)
    assert arr.shape == (6, 7) and info["bom"]
    for body, msg in ((b"1,2,3\n1,2\n1,2,3\n1,2,3\n1,2,3\n", "行列不一致"),
                      (b"1,2,3,4\n1,,3,4\n1,2,3,4\n1,2,3,4\n", "缺失"),
                      (b"1,2,3,4\n1,x,3,4\n1,2,3,4\n1,2,3,4\n", "认不出"),
                      (b"1,2,3,4\n1,nan,3,4\n1,2,3,4\n1,2,3,4\n", "缺失")):
        p = tmp_path / "b.txt"
        p.write_bytes(codecs.BOM_UTF8 + body)
        with pytest.raises(frame.FrameError, match=msg):
            frame.read_matrix(p)
    a = np.full((10, 10), 1.2)
    a[0, :3] = 0
    fixed, rep = frame.check_range(a, 0.3, 3.0, 0.05)
    assert rep["out_of_range_px"] == 3 and fixed.min() == pytest.approx(1.2)
    a[:2] = 0
    with pytest.raises(frame.FrameError, match="范围不合理"):
        frame.check_range(a, 0.3, 3.0, 0.05)


def test_qvdf_formats(tmp_path):
    ini = tmp_path / "point1.qvdf"
    ini.write_text("[Acquisition]\nobjective=20X,100X\nintensity=1sun\nframes=60\n"
                   "pixel_size_um_20X=0.293\npixel_size_um_100X=0.0586\n", encoding="utf-8")
    f = frame.read_qvdf(ini)["fields"]
    assert f["objectives"] == ["20X", "100X"] and f["frames"] == 60
    assert f["pixel_size_um_by_objective"] == {"20X": 0.293, "100X": 0.0586}
    js = tmp_path / "a.qvdf"
    js.write_text('{"acq": {"Magnification": "100x", "pixel_size_um": 0.06}}', encoding="utf-8")
    assert frame.read_qvdf(js)["fields"]["objective"] == "100X"
    xml = tmp_path / "b.qvdf"
    xml.write_text("<qv><objective>20X</objective><laser>405R</laser></qv>", encoding="gbk")
    assert frame.read_qvdf(xml)["fields"]["laser"] == "405R"
    assert frame.read_qvdf(None)["found"] is False


# ------------------------------------------------------------------ 指标
def _brute(mask, conn):
    lab = -np.ones(mask.shape, int)
    n = 0
    nb = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)] if conn == 8 else \
         [(-1, 0), (1, 0), (0, -1), (0, 1)]
    for i, j in zip(*np.nonzero(mask)):
        if lab[i, j] >= 0:
            continue
        stack, lab[i, j] = [(i, j)], n
        while stack:
            y, x = stack.pop()
            for dy, dx in nb:
                yy, xx = y + dy, x + dx
                if 0 <= yy < mask.shape[0] and 0 <= xx < mask.shape[1] and mask[yy, xx] and lab[yy, xx] < 0:
                    lab[yy, xx] = n
                    stack.append((yy, xx))
        n += 1
    return n, sorted(np.bincount(lab[lab >= 0]).tolist())


@pytest.mark.parametrize("conn", [8, 4])
def test_connected_components_match_flood_fill(conn):
    mask = np.random.default_rng(3).random((40, 55)) < 0.35
    cc = metrics.components(mask, conn)
    n, areas = _brute(mask, conn)
    assert cc["n"] == n and sorted(cc["area"].astype(int).tolist()) == areas


def test_morans_i_and_variogram_tell_random_from_structure():
    rng = np.random.default_rng(1)
    noise = 1.2 + 0.005 * rng.standard_normal((304, 456))
    yy, xx = np.mgrid[:304, :456]
    grad = noise + 0.02 * xx / 456
    assert abs(metrics.morans_i(noise)) < 0.1
    assert metrics.morans_i(grad) > 0.5
    vn, vg = metrics.variogram(noise), metrics.variogram(grad)
    assert vn["reached"] and vn["range_px"] <= 4
    assert not vg["reached"] or vg["range_px"] > 40


def test_metrics_find_scratches_but_not_edges():
    rng = np.random.default_rng(2)
    a = 1.2 + 0.004 * rng.standard_normal((304, 456))
    a[150:152, 100:350] -= 0.06                       # 一条划痕
    m, extra = metrics.compute(a)
    assert m["scratch_count"] == 1 and m["scratches"][0]["length_px"] > 200
    assert any(f["code"] == "scratch" for f in m["flags"])
    b = 1.2 + 0.004 * rng.standard_normal((304, 456))
    yy, xx = np.mgrid[:304, :456]
    b -= 0.05 * np.exp(-np.minimum.reduce([yy, 303 - yy, xx, 455 - xx]) / 20)
    mb, _ = metrics.compute(b)
    assert mb["scratch_count"] == 0 and mb["edge_minus_center"] < -0.02
    assert "edge_effect" in {f["code"] for f in mb["flags"]}
    clean, _ = metrics.compute(1.2 + 0.004 * rng.standard_normal((304, 456)))
    assert clean["grade"] == "A" and clean["score"] > m["score"] and not clean["flags"]
    assert clean["low_2s_frac"] == pytest.approx(0.023, abs=0.01)
    assert clean["low_abs_frac"] == 0 and metrics.compute(a - 0.2)[0]["low_abs_frac"] == 1


def test_score_and_grade_cutoffs():
    P = metrics.PARAMS
    assert [metrics.grade(s, P) for s in (100, 85, 84.9, 70, 55, 54.9, None)] == ["A", "A", "B", "B", "C", "D", None]


# ------------------------------------------------------------------ 整条流水线
def test_pipeline_writes_an_independent_folder_and_never_touches_raw(workspace, data):
    before = _tree_hash(data)
    run = pipeline.process(data, figures=False)
    assert _tree_hash(data) == before                        # 原始数据一个字节都没动
    out = pipeline.runs_dir() / run["run_id"]
    assert out.parent == workspace / "qfls"
    for f in pipeline.RUN_FILES:
        assert (out / f).is_file(), f
    assert run["status"] == "ok" and run["n_frames"] == 60 and run["n_valid"] == 58
    pts = pipeline.load(run["run_id"], "points.json")
    bad = [p for p in pts if not p["valid"]]
    assert len(bad) == 2 and all(p["grade"] is None and p["score"] is None and p["level"] == "error" for p in bad)
    assert {p["error"].split("：")[0] for p in bad} == {"行列不一致", "有缺失值"}
    good = [p for p in pts if p["valid"]]
    assert [p["rank"] for p in good] == list(range(1, 59))
    assert all(good[i]["score"] >= good[i + 1]["score"] for i in range(57))
    codes = {c for p in pts for c in p["flag_codes"]}
    assert {"out_of_range", "shape_differs", "meta_mismatch", "no_qvdf", "scratch", "edge_effect",
            "group_outlier", "structure", "large_low_area"} <= codes
    # 组内 z：同实验同物镜
    z = [p["z"]["median"] for p in good if p["z"]["median"] is not None]
    assert z and abs(np.mean(z)) < 0.5
    s = pipeline.load(run["run_id"], "summary.json")
    assert s["objectives"] == ["100X", "20X"] and set(s["color_scales"]) == {"20X", "100X"}
    assert sum(s["grades_total"].values()) == 58
    assert "不同物镜的绝对 QFLS" in (out / "README.md").read_text(encoding="utf-8")
    with (out / "points.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 60 and "z_median" in rows[0] and rows[0]["rank"] == "1"


def test_csv_guards_formulas(workspace, tmp_path):
    root = tmp_path / "r"
    q = root / "=HYPERLINK(1)" / "abc123" / "slot1" / "point1" / "QFLS"
    q.mkdir(parents=True)
    demo.write_txt(q / "0-20X-405R-1sun-20261101120000-0123456789abcdef0123456789abcdef.txt",
                   1.2 + 0.004 * np.random.default_rng(0).standard_normal((48, 72)))
    run = pipeline.process(root, figures=False)
    text = (pipeline.runs_dir() / run["run_id"] / "points.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK(1)" in text


def test_root_checks(workspace, tmp_path):
    with pytest.raises(ValueError, match="不是一个文件夹"):
        pipeline.check_root(str(tmp_path / "nope"))
    pipeline.runs_dir().mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="输出目录"):
        pipeline.check_root(str(pipeline.runs_dir()))


# ------------------------------------------------------------------ 接口
def _wait(c, task_id, limit=120):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        t = c.get(f"/api/tasks/{task_id}").json()
        if t["done"]:
            return t
        time.sleep(0.2)
    raise AssertionError("任务没跑完")


def test_api_demo_run_points_files(workspace, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(demo, "SMALL", TINY)
    with TestClient(app) as c:
        # 演示数据：生成 + 处理（不出图，快）
        r = c.post("/api/qfls/runs", json={"demo": True, "figures": False}).json()
        t = _wait(c, r["task"]["task_id"])
        assert t["status"] == "ok", t.get("error")
        rid = r["run_id"]
        assert c.get("/api/qfls/runs").json()["runs"][0]["run_id"] == rid
        d = c.get(f"/api/qfls/runs/{rid}").json()
        assert d["run"]["status"] == "ok" and d["summary"]["n_frames"] == 60 and d["scan"]["renamed"]
        assert d["run"]["root"] == str(pipeline.demo_dir().resolve())
        pts = c.get(f"/api/qfls/runs/{rid}/points").json()["points"]
        assert len(pts) == 60 and c.get(f"/api/qfls/runs/{rid}/points/{pts[0]['key']}").json()["images"] == []
        sc = c.post("/api/qfls/scan", json={"root": d["run"]["root"]}).json()
        assert sc["n_frames"] == 60 and sc["objectives"] == ["100X", "20X"]

        # 自己的目录：两帧，出图
        q = tmp_path / "mine" / "exp" / "abc123" / "slot1" / "point1" / "QFLS"
        q.mkdir(parents=True)
        a = 1.2 + 0.004 * np.random.default_rng(0).standard_normal((96, 144))
        a[40:42, 20:120] -= 0.06
        demo.write_txt(q / "0-20X-405R-1sun-20261101120000-0123456789abcdef0123456789abcdef.txt", a)
        r2 = c.post("/api/qfls/runs", json={"root": str(tmp_path / "mine")}).json()
        assert _wait(c, r2["task"]["task_id"])["status"] == "ok"
        rid2 = r2["run_id"]
        key = c.get(f"/api/qfls/runs/{rid2}/points").json()["points"][0]["key"]
        one = c.get(f"/api/qfls/runs/{rid2}/points/{key}").json()
        assert {"heatmap.png", "hist.png", "profile.png", "anomaly.png", "variogram.png", "summary.png",
                "heatmap_group.png"} <= set(one["images"])
        assert one["extra"]["hist"]["counts"] and one["point"]["scratches"]
        png = c.get(f"/api/qfls/runs/{rid2}/files/points/{key}/heatmap.png")
        assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
        assert c.get(f"/api/qfls/runs/{rid2}/files/global/boxplots.png").status_code == 200
        assert c.get(f"/api/qfls/runs/{rid2}/files/points.csv").headers["content-type"].startswith("text/csv")
        assert c.get(f"/api/qfls/runs/{rid2}/files/../../hte.db").status_code == 404
        assert c.get(f"/api/qfls/runs/{rid2}/points/..%2f..").status_code == 404
        assert c.get("/api/qfls/runs/qr_nope").status_code == 404
        assert c.post("/api/qfls/runs", json={"root": "/no/such"}).status_code == 400
        assert c.post("/api/qfls/runs", json={"demo": True, "params": {"nope": 1}}).status_code == 400
        assert c.get("/api/qfls/method").json()["params"]["moran_threshold"] == 0.15
        assert c.delete(f"/api/qfls/runs/{rid}").json()["ok"]
        assert [x["run_id"] for x in c.get("/api/qfls/runs").json()["runs"]] == [rid2]


def test_demo_known_answers_are_all_found(workspace, tmp_path):
    """演示数据里造进去的每一类问题都要被标出来（演示数据的意义就在这）。用默认尺寸 304 × 456。"""
    demo.generate(tmp_path / "d")
    run = pipeline.process(tmp_path / "d", figures=False)
    pts = pipeline.load(run["run_id"], "points.json")
    flags = {c for p in pts for c in p["flag_codes"]}
    for what, flag, _where in demo.DESIGNED:
        assert flag in flags, what
    scratched = [p for p in pts if p["valid"] and p["experiment"] == demo.E3 and p["slot_no"] == 3]
    assert [p["scratch_count"] for p in scratched] == [2] * 6
    assert not [p for p in pts if p["valid"] and p["scratch_count"] and p not in scratched]   # 没有误报
    pair = {}
    for p in pts:
        if p["valid"]:
            pair.setdefault((p["experiment"], p["batch"], p["slot_no"], p["point_no"]), {})[p["objective"]] = p["median"]
    d = [v["100X"] - v["20X"] for v in pair.values() if len(v) == 2]
    assert len(d) >= 10 and abs(np.mean(d) + 0.024) < 0.002           # 造的物镜差是 −24 meV


# ------------------------------------------------------------------ 体检修的（C069）
def test_param_overrides_are_validated_and_deep_merged():
    P = pipeline.merge_params({"score": {"scratch": {"per": 5}}, "low_abs_ev": 1.12})
    assert P["score"]["scratch"] == {"max": 20, "per": 5}            # 只改一项，其余照旧
    assert P["score"]["spread"]["max"] == 25 and P["low_abs_ev"] == 1.12
    for bad, msg in (({"valid_range_ev": 5}, "2 个数的列表"), ({"nope": 1}, "没有这个参数"),
                     ({"score": {"spread": {"oops": 1}}}, "score.spread.oops"), ({"sigma_k": "2"}, "应该是数字"),
                     ({"score": 3}, "一组键值")):
        with pytest.raises(ValueError, match=msg):
            pipeline.merge_params(bad)


def test_interrupted_runs_are_reaped_and_ids_do_not_collide(workspace, tmp_path):
    d = pipeline.runs_dir() / "qr_x"
    d.mkdir(parents=True)
    (d / "run.json").write_text('{"run_id": "qr_x", "status": "running"}', encoding="utf-8")
    assert pipeline.reap_interrupted() == 1
    assert pipeline.load("qr_x", "run.json")["status"] == "failed"
    assert len({pipeline.new_run_id() for _ in range(200)}) == 200


def test_api_rejects_bad_params_and_reads_string_flags(workspace, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import qfls as qapi
    from app.main import app
    assert qapi._flag("false") is False and qapi._flag("0") is False and qapi._flag("yes") is True
    with TestClient(app) as c:
        r = c.post("/api/qfls/runs", json={"demo": True, "params": {"valid_range_ev": 5}})
        assert r.status_code == 400 and "列表" in r.json()["error"]["message"]
