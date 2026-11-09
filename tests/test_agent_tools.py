"""智能体的文件、批处理、任务工具（P-49：要能读写文件、跑批处理、管任务）。

文件那一半盯的是「越界」和「改坏了回不来」：
- 名单外的路径（`../`、绝对路径、软链）一律拒绝；工作区里 raw / config / modules / 数据库只读
- 读出来的密钥先打码（模型那头是外部网关）
- 每次写之前备份，撤销能还原；文件之后又被改过就拒绝撤销
批处理那一半盯的是「真的跑了、等得到、停得下」。
"""
import json
import os
import time
from pathlib import Path

import pytest

from app import config, tasks
from app.agent import files, loop, tools
from app.storage import agent_sessions as sessions
from app.storage import db
from test_batch import _matrix_file, imported  # noqa: F401 — imported 是 fixture


def _run(name, **args):
    r = tools.dispatch(name, args)
    return r["ok"], (json.loads(r["content"]) if r["ok"] else r["content"])


# ------------------------------------------------------------------ 范围
def test_relative_paths_live_in_the_workspace_and_nothing_escapes(workspace, tmp_path):
    (config.WORKSPACE / "agent").mkdir(exist_ok=True)
    assert files.resolve("agent/x.txt") == Path(os.path.realpath(config.WORKSPACE / "agent" / "x.txt"))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    for bad in ("../outside.txt", str(outside), "agent/../../outside.txt"):
        with pytest.raises(files.FileToolError, match="不在能碰的文件夹里"):
            files.resolve(bad)
    # 软链指到外面：按真实路径核，逃不出去
    link = config.WORKSPACE / "agent" / "link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器建不了软链")
    with pytest.raises(files.FileToolError, match="不在能碰的文件夹里"):
        files.resolve("agent/link/outside.txt")


@pytest.mark.parametrize("target", ["raw/x.csv", "config/providers.json", "modules/evil/module.py",
                                    "hte.db", "tables/t.parquet", "agent_backups/x"])
def test_platform_owned_parts_of_the_workspace_are_read_only(workspace, target):
    ok, msg = _run("write_file", path=target, content="x")
    assert not ok and "只读" in msg


def test_added_folders_are_read_only_unless_you_say_so(workspace, tmp_path):
    data = tmp_path / "mydata"
    data.mkdir()
    (data / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    db.set_setting("agent.roots", [{"path": str(data), "write": False}])
    ok, r = _run("read_file", path=str(data / "a.txt"))
    assert ok and "第二行" in r["content"] and r["lines"] == 2
    ok, msg = _run("write_file", path=str(data / "b.txt"), content="x")
    assert not ok and "只读" in msg
    db.set_setting("agent.roots", [{"path": str(data), "write": True}])
    ok, r = _run("write_file", path=str(data / "b.txt"), content="新的")
    assert ok and (data / "b.txt").read_text(encoding="utf-8") == "新的"


def test_new_folders_must_exist_and_not_be_everything(workspace, tmp_path):
    for bad, msg in ((str(tmp_path / "nope"), "不存在"), ("relative/dir", "完整路径"),
                     (str(Path.home()), "太宽"), (os.path.abspath(os.sep), "太宽")):
        with pytest.raises(files.FileToolError, match=msg):
            files.check_new_root(bad)


# ------------------------------------------------------------------ 读
def test_reading_masks_secrets_before_the_model_sees_them(workspace):
    config.PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PROVIDERS_PATH.write_text(json.dumps({"providers": {"gw": {
        "baseUrl": "http://user:pa55word@gw/v1", "apiKey": "sk-supersecretvalue123"}}}), encoding="utf-8")
    ok, r = _run("read_file", path="config/providers.json")
    assert ok
    assert "supersecretvalue" not in r["content"] and "pa55word" not in r["content"]
    assert "apiKey" in r["content"] and "****" in r["content"]
    ok, g = _run("search_files", pattern="sk-", path="config")
    assert ok and g["hits"] and all("supersecret" not in h["text"] for h in g["hits"])


def test_reading_handles_utf16_paging_and_binaries(workspace):
    d = config.WORKSPACE / "agent"
    d.mkdir(exist_ok=True)
    (d / "u16.csv").write_bytes("波长,吸光度\n500,0.12\n".encode("utf-16"))
    ok, r = _run("read_file", path="agent/u16.csv")
    assert ok and "吸光度" in r["content"] and r["encoding"].startswith("utf-16")
    (d / "long.txt").write_text("\n".join(f"行{i}" for i in range(50)), encoding="utf-8")
    ok, r = _run("read_file", path="agent/long.txt", offset=10, limit=5)
    assert r["from"] == 11 and r["to"] == 15 and r["next_offset"] == 15 and "   11  行10" in r["content"]
    (d / "b.bin").write_bytes(bytes(range(256)) * 4)
    ok, r = _run("read_file", path="agent/b.bin")
    assert ok and r["binary"] is True and "content" not in r


def test_list_and_search(workspace):
    d = config.WORKSPACE / "agent" / "sub"
    d.mkdir(parents=True)
    (d / "notes.md").write_text("膜厚 偏薄\n其它\n", encoding="utf-8")
    (d / "data.csv").write_text("a,b\n1,膜厚\n", encoding="utf-8")
    ok, r = _run("list_files", path="agent", depth=2)
    assert {e["path"] for e in r["entries"]} >= {os.path.join("agent", "sub"), os.path.join("agent", "sub", "notes.md")}
    ok, g = _run("search_files", pattern="膜厚", path="agent", glob="*.md")
    assert [(h["path"], h["line"]) for h in g["hits"]] == [(os.path.join("agent", "sub", "notes.md"), 1)]
    ok, msg = _run("search_files", pattern="([", path="agent")
    assert not ok and "正则写错了" in msg


# ------------------------------------------------------------------ 写、记录、撤销
def test_write_backs_up_and_rollback_restores(workspace):
    tools._ctx.session_id = "agt_test"
    try:
        ok, r = _run("write_file", path="agent/report.md", content="第一版\n结论：A\n", note="新建")
        assert ok and r["created"] and (config.WORKSPACE / "agent" / "report.md").exists()
        ok, r2 = _run("write_file", path="agent/report.md", find="结论：A", replace="结论：B")
        assert ok and "- 结论：A" in r2["diff"] and "+ 结论：B" in r2["diff"]
        assert Path(config.WORKSPACE / r2["backup"]).read_text(encoding="utf-8") == "第一版\n结论：A\n"
        # 片段不唯一 / 找不到 / 改完一样：都说清楚，不写
        _run("write_file", path="agent/dup.txt", content="x\nx\n")
        assert "不唯一" in _run("write_file", path="agent/dup.txt", find="x", replace="y")[1]
        assert "找不到" in _run("write_file", path="agent/report.md", find="没有的", replace="y")[1]
        assert "一模一样" in _run("write_file", path="agent/report.md", content="第一版\n结论：B\n")[1]

        ok, ch = _run("file_changes")
        assert [c["path"] for c in ch["changes"]][:2] == [os.path.join("agent", "dup.txt"), os.path.join("agent", "report.md")]
        ok, rb = _run("rollback_file", change_id=r2["change_id"])
        assert ok and rb["restored"] == [os.path.join("agent", "report.md")]
        assert (config.WORKSPACE / "agent" / "report.md").read_text(encoding="utf-8") == "第一版\n结论：A\n"
        # 撤销整个会话：新建的文件被删掉
        ok, rb = _run("rollback_file", all_in_this_session=True)
        assert not (config.WORKSPACE / "agent" / "report.md").exists()
        assert not (config.WORKSPACE / "agent" / "dup.txt").exists()
    finally:
        tools._ctx.session_id = None


def test_edits_keep_the_files_encoding_and_do_not_add_a_bom(workspace):
    d = config.WORKSPACE / "agent"
    d.mkdir(exist_ok=True)
    (d / "u8.txt").write_bytes("阈值 = 2.0\n".encode("utf-8"))
    (d / "bom.txt").write_bytes("阈值 = 2.0\n".encode("utf-8-sig"))
    (d / "gb.txt").write_bytes("阈值 = 2.0\n".encode("gb18030"))
    (d / "u16.txt").write_bytes("阈值 = 2.0\n".encode("utf-16"))
    for name in ("u8.txt", "bom.txt", "gb.txt", "u16.txt"):
        ok, _ = _run("write_file", path=f"agent/{name}", find="2.0", replace="2.5")
        assert ok, name
    assert (d / "u8.txt").read_bytes() == "阈值 = 2.5\n".encode("utf-8")          # 没有凭空多一个 BOM
    assert (d / "bom.txt").read_bytes() == "阈值 = 2.5\n".encode("utf-8-sig")
    assert (d / "gb.txt").read_bytes() == "阈值 = 2.5\n".encode("gb18030")
    assert (d / "u16.txt").read_bytes() == "阈值 = 2.5\n".encode("utf-16")


def test_rollback_refuses_when_someone_changed_the_file_since(workspace):
    ok, r = _run("write_file", path="agent/a.txt", content="智能体写的")
    (config.WORKSPACE / "agent" / "a.txt").write_text("你后来手改的", encoding="utf-8")
    ok, rb = _run("rollback_file", change_id=r["change_id"])
    assert ok and rb["restored"] == [] and "又被改过" in rb["skipped"][0]
    assert (config.WORKSPACE / "agent" / "a.txt").read_text(encoding="utf-8") == "你后来手改的"
    ok, rb = _run("rollback_file", change_id=r["change_id"], force=True)
    assert rb["restored"] and not (config.WORKSPACE / "agent" / "a.txt").exists()


def test_write_preview_shows_the_diff_and_writes_nothing(workspace):
    p = config.WORKSPACE / "agent" / "cfg.txt"
    p.parent.mkdir(exist_ok=True)
    p.write_text("阈值 = 2.0\n", encoding="utf-8")
    pv = tools.preview("write_file", {"path": "agent/cfg.txt", "find": "2.0", "replace": "2.5", "note": "放宽"})
    assert "改文件 agent" in pv and "- 阈值 = 2.0" in pv and "+ 阈值 = 2.5" in pv and "放宽" in pv
    assert p.read_text(encoding="utf-8") == "阈值 = 2.0\n"


# ------------------------------------------------------------------ 批处理与任务
def test_run_batch_needs_a_scope_and_its_preview_says_what_it_will_do(imported):  # noqa: F811
    ok, msg = _run("run_batch")
    assert not ok and "要指定样品范围" in msg
    pv = tools.preview("run_batch", {"batch": "B21", "recipe": {"band_min": 780}})
    assert "对 4 个样品跑一次批处理（批次 B21）" in pv and "band_min：775.0 → 780" in pv
    pv = tools.preview("run_batch", {"batch": "B21"})
    assert "配方：默认（膜厚窗口 775.0–1120.0 nm" in pv
    assert "没命中" in tools.preview("run_batch", {"batch": "B99"})


def test_run_batch_then_wait_then_summarise(imported):  # noqa: F811
    ok, r = _run("run_batch", batch="B21", title="智能体跑的")
    assert ok and r["n_samples"] == 4
    ok, t = _run("get_task", task_id=r["task_id"], wait_seconds=60)
    assert ok and t["done"] and t["status"] == "ok" and t["run_id"] and t["n_ok"] == 4
    ok, s = _run("batch_summary", run_id=t["run_id"])
    assert ok and s["title"] == "智能体跑的" and s["n_ok"] == 4 and s["n_failed"] == 0
    ot = {f["field_name"]: f for f in s["fields"]}
    assert ot and all(f["n"] == 4 for f in ot.values())
    ok, lt = _run("list_tasks")
    assert lt["tasks"][0]["task_id"] == r["task_id"]


def test_waiting_stops_when_the_user_presses_stop(imported):  # noqa: F811
    ok, r = _run("run_batch", all_samples=True)
    tools._ctx.should_stop = lambda: True
    try:
        t0 = time.monotonic()
        ok, t = _run("get_task", task_id=r["task_id"], wait_seconds=60)
        assert ok and time.monotonic() - t0 < 2
    finally:
        tools._ctx.should_stop = None
        _wait_done(r["task_id"])


def test_cancel_task_preview_and_cancel(imported):  # noqa: F811
    ok, r = _run("run_batch", all_samples=True)
    pv = tools.preview("cancel_task", {"task_id": r["task_id"]})
    assert "已经跑完的样品结果保留" in pv or "已经结束" in pv
    ok, c = _run("cancel_task", task_id=r["task_id"])
    assert ok and "停在这儿" in c["note"]
    assert _wait_done(r["task_id"])["status"] in ("cancelled", "ok")
    assert "不存在" in tools.preview("cancel_task", {"task_id": "task_nope"})


def _wait_done(task_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = tasks.get(task_id)
        if t["done"]:
            return t
        time.sleep(0.05)
    raise AssertionError("任务超时")


# ------------------------------------------------------------------ 回路里：写文件要批，停止立刻停
def test_write_file_goes_through_approval_in_the_loop(workspace, monkeypatch):
    import httpx

    from app.ai import gateway
    from test_agent import PROVIDER, Gateway, calls, say

    g = Gateway(calls(("write_file", {"path": "agent/x.md", "content": "你好\n"})), say("写好了"))
    monkeypatch.setattr(gateway, "client_factory", lambda timeout=None: httpx.Client(transport=httpx.MockTransport(g)))
    sid = sessions.create()
    ev = list(loop.run(sid, provider=PROVIDER, model="fake", prompt="写个文件"))
    ap = next(e for e in ev if e["type"] == "approval")
    assert "新建文件 agent" in ap["preview"] and "+ 你好" in ap["preview"]
    assert not (config.WORKSPACE / "agent" / "x.md").exists()            # 没批之前不动
    ev2 = list(loop.run(sid, provider=PROVIDER, model="fake", resume={"decisions": {ap["id"]: "approve"}}))
    res = next(e for e in ev2 if e["type"] == "tool_result")
    assert res["ok"] and (config.WORKSPACE / "agent" / "x.md").read_text(encoding="utf-8") == "你好\n"
    # 改动记在这个会话名下
    assert files.changes(sid)[0]["path"] == os.path.join("agent", "x.md")


def test_api_roots_and_ui_rollback(workspace, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    data = tmp_path / "d"
    data.mkdir()
    with TestClient(app) as c:
        st = c.post("/api/agent/settings", json={"roots": [{"path": str(data), "write": True}]}).json()
        assert [r["path"] for r in st["roots"]][1:] == [os.path.realpath(data)]
        assert c.post("/api/agent/settings", json={"roots": [{"path": str(tmp_path / "nope")}]}).status_code == 400
        ok, r = _run("write_file", path=str(data / "n.txt"), content="x")
        assert c.post(f"/api/agent/file-changes/{r['change_id']}/rollback").json()["restored"]
        assert not (data / "n.txt").exists()
        sid = sessions.create()
        assert c.post(f"/api/agent/sessions/{sid}/stop").json() == {"ok": True, "running": False}
