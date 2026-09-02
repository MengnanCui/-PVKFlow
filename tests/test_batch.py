"""批处理与后台任务。

最重要的一条：**上千个样品里一定有跑失败的**，失败必须被隔离、被记录、
不能拖垮整批。
"""
import io
import os
import subprocess
import sys
import time
import zipfile

import numpy as np
import pytest

from app import batch, config, tasks
from app.storage import db, ingest, selection


def _matrix_file(path, n_lam=120, n_t=40, ot=4000.0, seed=0):
    """写一个光谱矩阵。

    seed 必须让每个文件的内容都不一样 —— 导入是按 sha256 内容去重的，
    两个字节完全相同的文件会被当成同一份（这是有意设计），
    夹具里如果偷懒复用内容，第二个样品根本建不出来。
    """
    rng = np.random.default_rng(seed)
    lam = np.linspace(600, 1100, n_lam)
    t = np.linspace(0, 10, n_t)
    with open(path, "w", encoding="utf-8") as f:
        f.write("Wavelength(nm)," + ",".join(f"{x:.3f}" for x in t) + "\n")
        for w in lam:
            row = 0.6 + 0.2 * np.cos(2 * np.pi * 2 * ot / w * (1 - 0.4 * t / 10))
            row = row + rng.normal(0, 0.002, row.shape)
            f.write(f"{w:.3f}," + ",".join(f"{v:.5f}" for v in row) + "\n")


@pytest.fixture()
def imported(workspace, tmp_path):
    """3 个批次 × 4 个样品，外加一个坏文件。"""
    src = tmp_path / "in"
    for bi, b in enumerate(("B20", "B21", "B22")):
        (src / b).mkdir(parents=True)
        for i in range(1, 5):
            _matrix_file(src / b / f"{b}_S{i}_absorbance.csv",
                         ot=3000 + 500 * i, seed=bi * 10 + i)
    # 故意放一个坏文件在 B20 里 —— 真实批次里就是会混进这种东西
    (src / "B20" / "B20_S9_absorbance.csv").write_text(
        "这不是矩阵\n随便写点什么\n", encoding="utf-8")

    prev = ingest.scan_preview(src)
    ingest.ingest_paths(prev["files"])
    with db.tx() as c:
        c.execute("UPDATE artifact SET is_matrix=1 WHERE kind='raw'")
    return src


def _wait(task_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = tasks.get(task_id)
        if t["done"]:
            return t
        time.sleep(0.05)
    raise AssertionError(f"任务超时：{tasks.get(task_id)}")


# ---------------------------------------------------------------- 样品身份
def test_same_sample_name_in_different_batches_stays_separate(imported):
    """命名规则把 B20_S1 拆成 batch=B20/sample=S1，S1 在每个批次都出现。

    只按名字唯一的话，三个批次的 S1 会被静默合并成一个样品 ——
    数据没丢但全串了，而且小数据集上根本看不出来。
    """
    assert db.scalar("SELECT COUNT(*) FROM sample WHERE name='S1'") == 3
    batches = {r["batch"] for r in db.query("SELECT batch FROM sample WHERE name='S1'")}
    assert batches == {"B20", "B21", "B22"}


# ---------------------------------------------------------------- 配方
def test_identical_file_contents_are_deduplicated(workspace, tmp_path):
    """内容寻址去重：两个字节完全相同的文件只登记一次。

    这是有意的（同一份数据换个名字导入不该存两遍），但要在报告里说出来，
    不能静默吞掉 —— 否则用户会以为第二个样品建好了。
    """
    src = tmp_path / "dup"
    src.mkdir()
    (src / "B20_S1_absorbance.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    (src / "B21_S1_absorbance.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    rep = ingest.ingest_paths(ingest.scan_preview(src)["files"]).as_dict()
    assert rep["counts"]["imported"] == 1
    assert rep["counts"]["duplicates"] == 1
    assert rep["duplicates"][0]["existing"]      # 说清楚跟谁重了


def test_recipe_rejects_inverted_bands():
    with pytest.raises(ValueError, match="膜厚窗口"):
        batch.Recipe.from_dict({"band_min": 1050, "band_max": 780})
    with pytest.raises(ValueError, match="积分波段"):
        batch.Recipe.from_dict({"integral_min": 950, "integral_max": 800})


def test_recipe_ignores_unknown_keys():
    r = batch.Recipe.from_dict({"integral_min": 700, "nonsense": 1})
    assert r.integral_min == 700


# ---------------------------------------------------------------- 执行
def test_batch_runs_and_writes_everything(imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20", "B21"]},
        "recipe": {"integral_min": 700, "integral_max": 900, "slope_center": 800},
    })["task_id"])

    assert t["status"] == "ok"
    res = t["result"]
    # B20 有 4 个好的 + 1 个坏的，B21 有 4 个好的
    assert res["n_ok"] == 8 and res["n_failed"] == 1
    assert res["n_total"] == 9

    detail = batch.batch_detail(res["parent_run_id"])
    assert len(detail["children"]) == res["n_total"]

    # 每个样品一条子运行，且都挂在父运行下
    assert db.scalar(
        "SELECT COUNT(*) FROM analysis_run WHERE parent_run_id=?",
        (res["parent_run_id"],)) == res["n_total"]

    # 标量进了 key_result —— 构效关系页立刻可用
    fields = {r["field_name"] for r in db.query(
        "SELECT DISTINCT field_name FROM key_result")}
    assert {"integral_initial", "integral_final", "integral_ratio"} <= fields


def test_long_table_holds_every_sample(imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]}, "recipe": {},
    })["task_id"])
    from app.storage import tabular

    tbl = tabular.read_table(t["result"]["table"]["table_id"])
    assert set(tbl["columns"]) >= {"sample_id", "sample_name", "batch", "t",
                                   "integral", "slope"}
    # 一张长表装下所有样品，而不是每个样品一个文件
    names = {row[tbl["columns"].index("sample_name")] for row in tbl["rows"]}
    assert len(names) == t["result"]["n_ok"]


def test_one_bad_sample_does_not_kill_the_batch(imported, tmp_path):
    """坏文件必须被隔离：其余样品照常跑完，坏的那个留下可读的错误。"""
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "B99_S1_absorbance.csv").write_text("完全不是矩阵\n", encoding="utf-8")
    ingest.ingest_paths(ingest.scan_preview(bad)["files"])
    with db.tx() as c:
        c.execute("UPDATE artifact SET is_matrix=1 WHERE filename LIKE 'B99%'")

    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20", "B99"]}, "recipe": {},
    })["task_id"])

    assert t["status"] == "ok"
    assert t["result"]["n_ok"] == 4          # B20 的 4 个好样品照常跑完
    assert t["result"]["n_failed"] == 2      # B20_S9（夹具里的坏文件）+ B99_S1

    detail = batch.batch_detail(t["result"]["parent_run_id"])
    failed = {(c["batch"], c["sample_name"]): c
              for c in detail["children"] if c["status"] == "failed"}
    assert set(failed) == {("B20", "S9"), ("B99", "S1")}
    for c in failed.values():
        assert c["error"], "失败的样品必须留下能读的原因"


def test_out_of_range_band_becomes_a_warning_not_a_crash(imported):
    """波段落在数据范围外：要警告，不要静默出 NaN，也不要炸。"""
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]},
        "recipe": {"integral_min": 200, "integral_max": 300},
    })["task_id"])
    detail = batch.batch_detail(t["result"]["parent_run_id"])
    warned = [c for c in detail["children"] if c["warnings"]]
    assert warned, "越界的波段应该产生警告"
    assert any("之外" in w for c in warned for w in c["warnings"])


def test_empty_selection_fails_loudly(imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["不存在的批次"]}, "recipe": {},
    })["task_id"])
    assert t["status"] == "failed"
    assert "没有命中" in t["error"]


# ---------------------------------------------------------------- 任务
def test_task_records_progress_and_result(imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID,
                           {"filter": {"batch": ["B20"]}, "recipe": {}})["task_id"])
    assert t["total"] > 0 and t["progress"] == t["total"]
    assert t["percent"] == 100.0
    assert t["n_ok"] + t["n_failed"] == t["total"]   # 每个样品都有交代
    assert t["n_ok"] == 4 and t["n_failed"] == 1


def test_unknown_task_kind_is_rejected(workspace):
    with pytest.raises(KeyError):
        tasks.submit("nope.not.registered", {})


def test_reap_interrupted_clears_stuck_tasks(workspace):
    """服务重启后，上次没跑完的不能永远显示 running。"""
    with db.tx() as c:
        c.execute("INSERT INTO task(task_id,kind,title,status,created_at)"
                  " VALUES('task_stuck','x','卡住的','running',?)", (db.now(),))
    assert tasks.reap_interrupted() == 1
    stuck = tasks.get("task_stuck")
    assert stuck["status"] == "failed" and "重启" in stuck["error"]


def test_cancel_stops_the_batch(imported):
    """取消是「停在这儿」，不是「当没发生过」—— 已跑完的结果保留。"""
    task = tasks.submit(batch.BATCH_SKILL_ID,
                        {"filter": {"has_matrix": True}, "recipe": {}})
    tasks.cancel(task["task_id"])
    t = _wait(task["task_id"])
    assert t["status"] in ("cancelled", "ok")     # 太快跑完也可能来不及取消


# ---------------------------------------------------------------- 导出脚本
def _export(imported_client, run_id, **params):
    r = imported_client.get(f"/api/batch/runs/{run_id}/export", params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(r.content))


@pytest.fixture()
def batch_client(imported):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.skills.registry import registry
    registry.load_all()
    with TestClient(app) as c:
        yield c


def test_export_bundles_script_data_and_readme(batch_client, imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20", "B21"]}, "recipe": {},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    z = _export(batch_client, run_id, column="integral", mode="overlay",
                group_by="batch")
    assert set(z.namelist()) == {"plot.py", "data.csv", "README.md"}

    script = z.read("plot.py").decode("utf-8")
    # 样式必须逐字来自规范，不是"差不多"的版本
    assert '"#2470a0"' in script and '"xtick.direction": "in"' in script
    assert '"savefig.dpi": 300' in script

    csv = z.read("data.csv").decode("utf-8-sig")
    header = csv.splitlines()[0]
    assert header == "sample_id,sample_name,batch,label,t,integral"
    # 长表：8 个成功样品的曲线都在里面
    import csv as csv_mod
    rows = list(csv_mod.DictReader(io.StringIO(csv)))
    assert len({r["sample_id"] for r in rows}) == t["result"]["n_ok"]


def test_export_slope_column_switches_label_and_data(batch_client, imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID,
                           {"filter": {"batch": ["B20"]}, "recipe": {}})["task_id"])
    z = _export(batch_client, t["result"]["parent_run_id"], column="slope",
                mode="band", group_by="none")
    script = z.read("plot.py").decode("utf-8")
    assert "Y_COLUMN = 'slope'" in script
    assert "CJK_OK" in script                 # 没有中文字体时退回英文标签
    assert "nanpercentile" in script          # band 模式画分位数带
    assert z.read("data.csv").decode("utf-8-sig").splitlines()[0].endswith(",slope")


def test_export_keeps_same_named_samples_from_different_batches_apart(
        batch_client, imported):
    """S1 在每个批次里都有一个 —— 导出必须按 sample_id 分曲线，不能按名字。

    按名字分组会把 B20/S1 和 B21/S1 悄悄合成一条：图少了一条曲线，
    而且那条合成的曲线里两个样品的时间轴是交错的，完全是假的。
    """
    import csv as csv_mod

    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20", "B21", "B22"]}, "recipe": {},
    })["task_id"])
    z = _export(batch_client, t["result"]["parent_run_id"],
                column="integral", mode="overlay", group_by="none")

    rows = list(csv_mod.DictReader(io.StringIO(z.read("data.csv").decode("utf-8-sig"))))
    by_name = {r["sample_name"] for r in rows}
    by_id = {r["sample_id"] for r in rows}
    assert "S1" in by_name and len(by_id) > len(by_name)   # 名字确实重了
    assert len(by_id) == t["result"]["n_ok"]

    # 脚本按 sample_id 分组，标签才用带批次的 label
    script = z.read("plot.py").decode("utf-8")
    assert 'groupby("sample_id"' in script
    assert 'groupby("sample_name"' not in script

    # 标题里的样品数是按身份算的，不是按名字
    assert f"{t['result']['n_ok']} 个样品" in script
    labels = {r["label"] for r in rows}
    assert "B20/S1" in labels and "B21/S1" in labels


def test_export_rejects_unknown_column(batch_client, imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID,
                           {"filter": {"batch": ["B20"]}, "recipe": {}})["task_id"])
    r = batch_client.get(f"/api/batch/runs/{t['result']['parent_run_id']}/export",
                         params={"column": "rm -rf"})
    # 400 + 这个平台自己那套错误结构。以前是 FastAPI 默认的 422 加一坨英文
    # pydantic 结构，前端认不出来，只能显示「请求失败（422）」。
    assert r.status_code == 400
    assert "只能是" in r.json()["error"]["message"]


@pytest.mark.parametrize("mode,group_by", [("overlay", "batch"),
                                           ("overlay", "none"),
                                           ("band", "none")])
def test_exported_script_actually_runs(batch_client, imported, tmp_path, mode, group_by):
    """真的把导出的 plot.py 跑一遍。

    导出脚本是"论文里那张图"的来源 —— 生成一份跑不起来的脚本比不给还糟。
    所以这里不检查字符串，直接解压、执行、看有没有出图。
    """
    matplotlib = pytest.importorskip("matplotlib", reason="脚本执行验证需要 matplotlib")

    t = _wait(tasks.submit(batch.BATCH_SKILL_ID,
                           {"filter": {"batch": ["B20"]}, "recipe": {}})["task_id"])
    z = _export(batch_client, t["result"]["parent_run_id"],
                column="integral", mode=mode, group_by=group_by)
    out = tmp_path / f"export-{mode}-{group_by}"
    z.extractall(out)

    env = {**os.environ, "MPLBACKEND": "Agg"}
    proc = subprocess.run([sys.executable, "plot.py"], cwd=out, env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr

    # 没有中文字体的机器上必须退回英文标签，而不是画一排豆腐块。
    # matplotlib 缺字形时只是 warn，图照出，所以只看返回码是发现不了的。
    assert "missing from font" not in proc.stderr, proc.stderr

    png = out / "figure.png"
    assert png.is_file() and png.stat().st_size > 5000, "出的图不能是空白占位"

    # 300 dpi：规范里写死的投稿分辨率
    from PIL import Image
    with Image.open(png) as im:
        assert im.info.get("dpi", (0, 0))[0] == pytest.approx(300, abs=1)


def test_run_and_file_listings_carry_the_batch(imported):
    """列表里必须能分出是哪个批次的样品。

    只回名字的话，24 个批次的 S9 在界面上长得一模一样 —— 前端拿不到批次，
    再怎么改也显示不出区别。所以这是查询的问题，不是渲染的问题。
    """
    from app.skills import runner
    from app.storage import artifacts

    _wait(tasks.submit(batch.BATCH_SKILL_ID,
                       {"filter": {"batch": ["B20", "B21"]}, "recipe": {}})["task_id"])

    runs = runner.recent_runs(50)
    assert runs and all("sample_batch" in r for r in runs)
    assert {r["sample_batch"] for r in runs if r["sample_batch"]} >= {"B20", "B21"}

    rows = artifacts.search(limit=50)["rows"]
    assert rows and all("sample_batch" in r for r in rows)


def test_curve_labels_carry_the_batch(batch_client, imported):
    """图例里只写 S1 的话，24 个批次的 S1 在图上分不出是哪一个。"""
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20", "B21"]}, "recipe": {},
    })["task_id"])
    r = batch_client.get(f"/api/batch/runs/{t['result']['parent_run_id']}/curves",
                         params={"column": "integral"})
    labels = {s["label"] for s in r.json()["series"]}
    assert "B20/S1" in labels and "B21/S1" in labels


def test_export_preview_returns_the_script_before_downloading(batch_client, imported):
    """下载之前先读一眼 —— 这份脚本是「论文里那张图」的来源。"""
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID,
                           {"filter": {"batch": ["B20"]}, "recipe": {}})["task_id"])
    r = batch_client.get(
        f"/api/batch/runs/{t['result']['parent_run_id']}/export/preview",
        params={"column": "integral", "mode": "band", "group_by": "none"})
    assert r.status_code == 200
    j = r.json()
    assert j["n_series"] == t["result"]["n_ok"]
    assert '"#2470a0"' in j["script"] and "nanpercentile" in j["script"]
    assert j["columns"] == ["sample_id", "sample_name", "batch", "label", "t", "integral"]
    # 预览出来的脚本必须跟 zip 里那份一模一样，不能是另写一份
    z = _export(batch_client, t["result"]["parent_run_id"],
                column="integral", mode="band", group_by="none")
    assert z.read("plot.py").decode("utf-8") == j["script"]


# ---------------------------------------------------------------- 对比历史
def test_run_title_lands_on_the_parent_run_not_just_the_task(batch_client, imported):
    """名字要跟着**父运行**落库。

    只放在 task 上的话，任务表清掉之后这次对比就没名字了 ——
    而对比历史读的正是父运行的 params。
    """
    r = batch_client.post("/api/batch/run", json={
        "filter": {"batch": ["B20"]}, "recipe": {}, "title": "干燥速率对比"})
    assert r.status_code == 200, r.text
    t = _wait(r.json()["task"]["task_id"])
    assert t["status"] == "ok"

    detail = batch.batch_detail(t["result"]["parent_run_id"])
    assert detail["run"]["params"]["title"] == "干燥速率对比"


def test_history_lists_runs_with_their_titles(batch_client, imported):
    """对比历史就是 /api/batch/runs —— 后端一直都在，缺的只是入口。"""
    import json as _json

    for name in ("第一次", "第二次"):
        r = batch_client.post("/api/batch/run", json={
            "filter": {"batch": ["B20"]}, "recipe": {}, "title": name})
        _wait(r.json()["task"]["task_id"])

    runs = batch_client.get("/api/batch/runs").json()["runs"]
    titles = [_json.loads(x["params_json"] or "{}").get("title") for x in runs]
    assert "第一次" in titles and "第二次" in titles
    # 最近的排前面 —— 历史列表按这个顺序显示
    assert titles[0] == "第二次"


def test_untitled_run_still_gets_a_usable_name(batch_client, imported):
    import json as _json

    r = batch_client.post("/api/batch/run",
                          json={"filter": {"batch": ["B20"]}, "recipe": {}})
    t = _wait(r.json()["task"]["task_id"])
    params = batch.batch_detail(t["result"]["parent_run_id"])["run"]["params"]
    assert params["title"]                      # 不能是空的
    assert "个样品" in params["title"]


# ------------------------------------------------------------------ 没有 pyarrow 也要能跑
#
# pyarrow 从必装清单里拿掉了：它一个人 131 MB，占整个安装的三成，
# 而实测一次 40 样品的批处理长表 Parquet 只比 CSV 省 0.58 MB。
# 于是 tabular.py 里那条「退回 CSV」的分支从**兜底**变成了**默认路径** ——
# 默认路径必须有测试。
def _no_parquet(monkeypatch):
    """让 to_parquet 表现得像 pyarrow 没装。"""
    import pandas as pd

    def boom(self, *a, **k):
        raise ImportError("Unable to find a usable engine; pyarrow 没装")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)


def test_long_table_falls_back_to_csv_without_pyarrow(workspace, monkeypatch):
    """写得进去、登记的路径是 .csv、读得回来，一行不少。"""
    import numpy as np
    import pandas as pd

    from app.storage import tabular

    from app.storage import results

    _no_parquet(monkeypatch)
    run_id = results.start_run(skill_id="t", skill_version="1", skill_name="t",
                               params={}, inputs=[], source="skill")
    df = pd.DataFrame({"sample_id": ["a"] * 5 + ["b"] * 5,
                       "t": np.tile(np.arange(5.0), 2),
                       "value": np.arange(10.0)})
    meta = tabular.write_table(run_id, "曲线长表", df)

    assert meta["path"].endswith(".csv"), "没退回 CSV"
    assert (config.WORKSPACE / meta["path"]).is_file()
    assert meta["n_rows"] == 10

    back = tabular.read_table(meta["table_id"])
    assert back["n_rows"] == 10
    assert back["columns"] == ["sample_id", "t", "value"]
    assert len(back["rows"]) == 10
    assert back["rows"][-1][2] == 9.0          # 数值原样读回来


def test_whole_batch_run_works_without_pyarrow(imported, monkeypatch):
    """★ 走完整条批处理链路，别只测 tabular 那一个函数。

    真正会坏的是「写的时候退回了 CSV，读的时候还按 Parquet 读」这种
    两头对不上 —— 只测写入侧看不出来。
    """
    from app.storage import tabular

    _no_parquet(monkeypatch)
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]},
        "recipe": {"integral_min": 700, "integral_max": 900, "slope_center": 800},
    })["task_id"])
    assert t["status"] == "ok"
    res = t["result"]
    assert res["n_ok"] >= 2

    tables = tabular.tables_for_run(res["parent_run_id"])
    assert tables, "批处理没落下长表"
    assert tables[0]["path"].endswith(".csv"), "没退回 CSV"

    back = tabular.read_table(tables[0]["table_id"])
    assert back["n_rows"] > 0
    assert len(back["columns"]) >= 3


# ---------------------------------------------------------------- 膜厚与时刻切片
def test_batch_thickness_matches_the_single_sample_page(batch_client, imported):
    """批处理里的膜厚必须和单样品页**逐点相同**。

    两处各写一份 FFT 的话，同一个样品在两个页面上会给出不同的膜厚，
    而且没有任何提示说该信哪个。所以批处理走的是同一个
    `fringe_ot.extract_series`，这条测试就是钉住「没人抄第二遍」。
    """
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]},
        # 夹具的合成谱只到 1100 nm，窗口必须落在里面
        "recipe": {"band_min": 775, "band_max": 1100},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    curves = batch_client.get(f"/api/batch/runs/{run_id}/curves?column=ot").json()
    assert curves["series"], "批处理没留下膜厚曲线"

    # 拿其中一个样品，走单样品页那条路再算一次
    row = db.query_one(
        "SELECT a.artifact_id, s.name FROM artifact a"
        "  JOIN sample s ON s.sample_id = a.sample_id"
        " WHERE s.name = ?", (curves["series"][0]["label"].split("/")[-1],))
    single = batch_client.get(
        f"/api/spectra/{row['artifact_id']}/thickness"
        "?lam_min=775&lam_max=1100").json()

    got = curves["series"][0]["y"]
    assert len(got) == len(single["y"])
    for a, b in zip(got, single["y"]):
        assert abs(a - b) < 0.01, "批处理和单样品页的膜厚对不上"


def test_slices_average_every_frame_and_report_how_many_were_trustworthy(
        batch_client, imported):
    """窗口内**全部帧**参与平均，可信比例单独给。

    不可信的帧会把均值拉偏。只给一个漂亮的数字、不说其中几帧靠谱，
    等于把「这个数能不能用」这个问题藏起来了。
    """
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]},
        # 夹具的合成谱只到 1100 nm，窗口必须落在里面
        "recipe": {"band_min": 775, "band_max": 1100},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    r = batch_client.get(
        f"/api/batch/runs/{run_id}/slices?windows=0:1,2:3").json()
    assert [(w["from"], w["to"]) for w in r["windows"]] == [(0.0, 1.0), (2.0, 3.0)]
    assert r["rows"]

    v = r["rows"][0]["values"][0]
    assert v["n_frames"] > 0
    assert v["mean"] is not None
    assert 0.0 <= v["ok_ratio"] <= 1.0
    assert v["n_ok"] <= v["n_frames"]


def test_a_window_past_the_end_says_so_instead_of_reusing_the_last_frame(
        batch_client, imported):
    """样品只测到 T 秒，问 T+100 秒时必须返回空并说明原因。

    拿最近的一帧顶替的话，「这批数据根本没测到那个时刻」这个事实就消失了 ——
    而它恰恰是你需要知道的。这类静默替换在小数据集上完全看不出来。
    """
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]},
        # 夹具的合成谱只到 1100 nm，窗口必须落在里面
        "recipe": {"band_min": 775, "band_max": 1100},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    r = batch_client.get(
        f"/api/batch/runs/{run_id}/slices?windows=0:1,9000:9001").json()
    for row in r["rows"]:
        assert row["values"][0]["mean"] is not None      # 窗口内有帧
        far = row["values"][1]
        assert far["mean"] is None
        assert far["n_frames"] == 0
        assert "超出" in far["note"] and str(int(row["t_max"])) in far["note"]


def test_a_broken_time_window_is_explained_not_ignored(batch_client, imported):
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]}, "recipe": {},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    r = batch_client.get(f"/api/batch/runs/{run_id}/slices?windows=abc")
    assert r.status_code == 400
    assert "abc" in r.json()["error"]["message"]         # 说清楚是哪一段看不懂

    r = batch_client.get(f"/api/batch/runs/{run_id}/slices?windows=1:2:3")
    assert r.status_code == 400


def test_a_run_without_thickness_says_to_rerun_instead_of_500(batch_client, workspace):
    """老的对比跑在加膜厚之前，没有那张表。要给出下一步，不是一个 404 空壳。"""
    from app.storage import results

    run_id = results.start_run(skill_id="x", skill_version="1", skill_name="x",
                               params={}, inputs=[], source="skill")
    results.finish_run(run_id, "ok")
    r = batch_client.get(f"/api/batch/runs/{run_id}/slices?windows=0:1")
    assert r.status_code == 404
    assert "重跑" in r.json()["error"]["message"]


# ---------------------------------------------------------------- 模块化迁移
def test_the_migrated_module_matches_the_old_hand_written_path(imported):
    """「特殊处理」从手写页面迁成模块之后，算出来的必须**逐点相同**。

    这是迁移的验收线。差一点点都不行 —— 用户拿着迁移前的图和迁移后的图
    对比时，看到的必须是同一条曲线，否则他没法相信这次重构什么都没动。
    """
    from app.modules.base import ModuleContext
    from app.modules.registry import registry as mreg
    from app.parsers import render

    mreg.load_all()
    mod = mreg.get("special.slope_integral")

    lam = np.linspace(600, 1100, 140)
    t = np.linspace(0, 10, 30)
    rng = np.random.default_rng(7)
    M = rng.normal(1.0, 0.3, (140, 30))

    params = {"integ": [800, 950], "slope_center": 950, "slope_half": 10}
    out = mod.compute(ModuleContext(lam, M, t, params))

    old_integ = render.band_integral(M, lam, 800, 950)
    old_slope = render.wavelength_slope(M, lam, 950, 10)

    np.testing.assert_allclose(out["integ"].y, old_integ, rtol=0, atol=0)
    np.testing.assert_allclose(out["slope"].y, old_slope, rtol=0, atol=0)


def test_batch_curves_now_come_from_the_module_declaration(imported):
    """长表里的 integral / slope 列是模块声明出来的，不是 batch.py 写死的。

    这条钉住的是「同事加一个模块，他的曲线自动进长表」—— 平台自己的
    特殊处理走的就是这条路，所以这条路一定是通的。
    """
    from app.modules.registry import registry as mreg
    from app.storage import tabular

    mreg.load_all()
    declared = {c.name for c in mreg.get("special.slope_integral").spec.batch_curves}
    assert declared == {"integral", "slope"}

    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]}, "recipe": {},
    })["task_id"])
    tbl = tabular.read_table(t["result"]["table"]["table_id"])
    assert declared <= set(tbl["columns"])


def test_a_module_that_crashes_does_not_take_the_whole_batch_down(imported, monkeypatch):
    """同事的模块崩了，你的其它结果照常拿得到 —— 只是那几列空着 + 一条 warning。

    不这样的话，没人敢在跑 200 个样品之前装别人的模块。
    """
    from app.modules.registry import registry as mreg

    mreg.load_all()
    mod = mreg.get("special.slope_integral")

    def boom(ctx):
        raise RuntimeError("同事写崩了")

    monkeypatch.setattr(type(mod), "compute", lambda self, ctx: boom(ctx))

    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]}, "recipe": {},
    })["task_id"])

    assert t["status"] == "ok"                 # 整批没被带走
    assert t["result"]["n_ok"] > 0
    detail = batch.batch_detail(t["result"]["parent_run_id"])
    warns = " ".join(w for c in detail["children"] for w in c["warnings"])
    assert "同事写崩了" in warns and "特殊处理" in warns    # 说清是哪个模块崩的


def test_thickness_columns_come_from_the_module_not_from_batch_py(imported):
    """膜厚长表的列由膜厚模块声明出来，`app/batch.py` 里没有第二份 FFT。

    迁移前 `fringe_ot.extract_series` 被调用了两次：单样品页一次、批处理一次。
    两份实现迟早会漂，而且漂了也不报错 —— 只是同一个样品在两个页面上
    给出不同的膜厚，没人知道该信哪个。这条钉住「只剩一处」。
    """
    import inspect

    from app.modules.registry import registry as mreg

    mreg.load_all()
    spec = mreg.get("thickness.fringe_ot").spec
    assert {c.name for c in spec.batch_curves} == {"ot", "ot_ok"}
    assert spec.batch_all_frames, "膜厚不能被 max_time_points 抽稀"

    src = inspect.getsource(batch)
    assert "extract_series" not in src, (
        "app/batch.py 里又出现了 extract_series —— 膜厚该只在模块里算一次")


def test_thickness_long_table_carries_the_per_frame_verdict(batch_client, imported):
    """判级跟着膜厚一起进长表，而且是模块算的那一份 0/1。

    「1 s 内的平均膜厚」光有均值没法判断可不可靠，所以每帧可不可信必须
    一起存下来 —— 否则 /slices 只能自己再判一次，那就是第二份判据。
    """
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]},
        "recipe": {"band_min": 775, "band_max": 1100},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    from app.api import batch as batch_api

    _detail, tbl, df = batch_api._load_curves(run_id, "ot")
    assert {"ot", "ot_ok"} <= set(df.columns)
    assert "status" not in df.columns, "判级现在是模块给的 0/1 列，不是字符串"

    r = batch_client.get(f"/api/batch/runs/{run_id}/slices?windows=0:100").json()
    for row in r["rows"]:
        g = df[df["sample_id"] == row["sample_id"]]
        want = int((g["ot_ok"] > 0.5).sum())
        assert row["values"][0]["n_ok"] == want, (
            f"{row['label']} 的可信帧数和长表对不上")


def test_subsampling_shortens_the_curves_but_never_the_thickness(imported):
    """max_time_points 抽稀 integral / slope，但**一帧都不能从膜厚里拿走**。

    膜厚是用来看干燥过程哪一秒变坏的。抽稀等于把答案抹掉，
    而且抹得悄无声息 —— 曲线照样画得出来，只是那个拐点没了。
    """
    row = db.query_one(
        "SELECT s.sample_id, s.name, s.batch, a.artifact_id AS matrix_id"
        "  FROM sample s JOIN artifact a ON a.sample_id = s.sample_id"
        " WHERE a.is_matrix = 1 AND s.batch = 'B20' LIMIT 1")
    recipe = batch.Recipe.from_dict(
        {"band_min": 775, "band_max": 1100, "max_time_points": 7})
    out = batch._process_one(dict(row), recipe)

    assert out.ok, out.error
    assert len(out.t) == 7, "integral / slope 该被抽稀"
    assert len(out.ot_t) == 40, "膜厚必须是全部帧"
    assert len(out.full_curves["ot"]) == 40
    assert len(out.full_curves["ot_ok"]) == 40


def test_batch_extra_is_how_undrawn_numbers_reach_the_batch(imported):
    """`Curve(key=...)` 从 `PanelData.batch_extra` 取数，不从画出来的曲线取。

    这是同事会用到的口子：算出来、不画在图上、但该进批处理的数字。
    膜厚的判级就走这条路。
    """
    from app.modules.base import Curve, PanelData

    d = PanelData(x=[0, 1], y=[3.0, 4.0], batch_extra={"flag": [1.0, 0.0]})
    assert d.column() == [3.0, 4.0]
    assert d.column("flag") == [1.0, 0.0]
    # 取不到要返回 None，不是空列表 —— 「没这一列」和「这一列是空的」得分得开
    assert d.column("nope") is None
    assert PanelData().column() is None
    assert Curve("ot_ok", from_panel="ot_band", key="ot_ok").as_dict()["key"] == "ot_ok"


# ------------------------------------------------------------------ 配方 ←→ 模块参数
#
# 对比页的参数面板现在照模块声明生成，值写进 recipe.module_params。
# 这几条盯的是「面板上改了、跑出来的真是那个值」这条链。
def test_module_params_sync_back_to_flat_fields():
    """从控件改的值要同步回扁平字段。

    生效的本来就是 module_params（_params_for 里它排在最后）。不同步的话，
    存下来的配方里 band_min 是旧值、module_params.band 是新值，
    下次打开这次对比，参数面板和实际算出来的结果对不上。
    """
    r = batch.Recipe.from_dict({
        "band_min": 780, "band_max": 1050,
        "module_params": {
            "thickness.fringe_ot": {"band": [820, 1100]},
            "special.slope_integral": {"slope_half": 25, "integ": [830, 940]},
        },
    })
    assert (r.band_min, r.band_max) == (820.0, 1100.0)
    assert (r.integral_min, r.integral_max) == (830.0, 940.0)
    assert r.slope_half_width == 25.0
    assert r.slope_center == 950.0          # 没给的那个不动


def test_module_params_still_go_through_validation():
    """从控件也能把值改坏，校验一样要拦住。"""
    with pytest.raises(ValueError, match="膜厚窗口不合法"):
        batch.Recipe.from_dict(
            {"module_params": {"thickness.fringe_ot": {"band": [1100, 820]}}})


def test_legacy_flat_recipe_still_reaches_modules():
    """存了几个月的老配方（只有扁平字段）不能因为迁移就读不出来。"""
    r = batch.Recipe.from_dict({"band_min": 700, "band_max": 900,
                                "slope_center": 940, "slope_half_width": 12})
    eff = batch.effective_params(r)
    assert eff["thickness.fringe_ot"]["band"] == [700.0, 900.0]
    assert eff["special.slope_integral"]["slope_center"] == 940.0
    assert eff["special.slope_integral"]["slope_half"] == 12.0


def test_effective_params_covers_every_installed_module():
    """参数面板照这份结果画控件 —— 少一个模块，它的参数在对比页上就够不着。"""
    from app.modules.registry import registry

    if not registry.all():
        registry.load_all()
    eff = batch.effective_params(batch.Recipe.from_dict({}))
    assert set(eff) == {m.spec.id for m in registry.all()}
    for m in registry.all():
        assert set(eff[m.spec.id]) >= set(m.spec.defaults())


def test_detail_carries_the_values_the_recipe_panel_should_show(
        batch_client, imported):
    """对比页照模块声明画控件，控件里填什么值由后端给。

    「默认值 ← 老配方 ← 显式指定」只有 _params_for 一份实现。前端自己再算一遍
    迟早会对不上 —— 那时候面板上显示的参数和真正跑出来的结果是两回事。
    """
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]},
        "recipe": {"band_min": 800, "band_max": 1090, "slope_half_width": 15},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    d = batch_client.get(f"/api/batch/runs/{run_id}").json()
    eff = d["module_params"]
    assert eff["thickness.fringe_ot"]["band"] == [800.0, 1090.0]
    assert eff["special.slope_integral"]["slope_half"] == 15.0


def test_axes_gives_the_range_the_sliders_need(batch_client, imported):
    """控件声明 range_from="lambda" 时滑块的上下限从这儿来。

    拿不到就退回控件自己声明的 min/max —— 所以这个接口**不抛错**，
    只说 available=false。为了画一根滑块而让整页报错是本末倒置。
    """
    t = _wait(tasks.submit(batch.BATCH_SKILL_ID, {
        "filter": {"batch": ["B20"]}, "recipe": {"band_min": 775, "band_max": 1100},
    })["task_id"])
    run_id = t["result"]["parent_run_id"]

    a = batch_client.get(f"/api/batch/runs/{run_id}/axes").json()
    assert a["available"] is True
    assert a["lambda"][0] < a["lambda"][1]
    assert a["time"][0] <= a["time"][1]

    miss = batch_client.get("/api/batch/runs/run_nope/axes")
    assert miss.status_code == 404          # 批处理本身不存在还是要说清楚


def test_sample_page_accepts_a_sample_id_not_just_a_matrix_id(batch_client, imported):
    """`#sample/smp_xxx` 要能打开。

    页面地址叫 `sample/`，而界面上能复制到的 id 常常就是 `smp_xxx`
    （样品列表、失败样品表里显示的都是它）。原来只收矩阵 id，
    回一句「文件不存在：smp_xxx」—— 那句话根本没说到点子上。
    """
    smp = batch_client.get("/api/spectra/samples").json()["samples"][0]
    by_sample = batch_client.get(f"/api/spectra/{smp['sample_id']}/meta")
    assert by_sample.status_code == 200, by_sample.text

    d = by_sample.json()
    # 回传**翻译之后**的 id：界面拿它去拉热力图、拉帧、算模块面板，
    # 回一个它认不出来的 id 等于后面每一步都要再翻译一次
    assert d["artifact_id"] == smp["matrices"][0]["artifact_id"]
    assert d == batch_client.get(f"/api/spectra/{d['artifact_id']}/meta").json()

    miss = batch_client.get("/api/spectra/smp_压根没有/meta")
    assert miss.status_code == 404
    assert "没有这个样品" in miss.json()["error"]["message"]
