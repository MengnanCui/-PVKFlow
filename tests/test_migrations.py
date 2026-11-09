"""数据库版本与迁移（app/storage/migrations.py）。

升级用的「老库」是 2026-08-26 第一个提交里的 schema.sql 原样建出来的
（tests/fixtures/schema_v0_2026-08-26.sql）—— 不是凭想象造一个旧结构。
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from app import config
from app.storage import migrations

FIXTURE = config.ROOT / "tests" / "fixtures" / "schema_v0_2026-08-26.sql"


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """一个空工作区，不预先建库（workspace 夹具会先 init，这里要自己控制）。"""
    monkeypatch.setenv("HTE_WORKSPACE", str(tmp_path / "ws"))
    from app.storage import db
    importlib.reload(config)
    importlib.reload(db)
    db.close()
    config.ensure_dirs()
    yield tmp_path / "ws"
    db.close()


def _old_workspace(ws_path):
    """按第一个提交的 schema 建库，再塞两行老数据。"""
    conn = sqlite3.connect(ws_path / "hte.db")
    conn.executescript(FIXTURE.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO sample(sample_id, name, batch, created_at)"
                 " VALUES('smp_old', 'S1', 'B20', '2026-08-26T00:00:00+00:00')")
    conn.execute("INSERT INTO analysis_run(analysis_run_id, skill_id, skill_version, status,"
                 " started_at) VALUES('run_old', 'x', '1', 'ok', '2026-08-26T00:00:00+00:00')")
    conn.commit()
    conn.close()


def test_a_fresh_workspace_starts_at_the_latest_version_without_a_backup(ws):
    from app.storage import db

    conn = db.init()
    assert migrations.current_version(conn) == migrations.LATEST
    assert not (ws / "backups").exists()
    hist = conn.execute("SELECT version, description FROM schema_migration").fetchall()
    assert len(hist) == 1 and "新建" in hist[0][1]


def test_an_old_workspace_is_backed_up_then_upgraded_without_losing_data(ws):
    from app.storage import db

    _old_workspace(ws)
    conn = db.init()

    assert migrations.current_version(conn) == migrations.LATEST
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analysis_run)")}
    assert "parent_run_id" in cols
    assert {r[1] for r in conn.execute("PRAGMA table_info(artifact)")} >= {"is_matrix"}
    # 第 4 步：审核留痕的两列
    assert {r[1] for r in conn.execute("PRAGMA table_info(key_result)")} >= {"quality_note", "quality_at"}
    # 后来加的表、视图都建出来了
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"task", "sample_set", "conversation", "key_result_current", "agent_session", "agent_file_change"} <= names
    # 老数据一行没少
    assert conn.execute("SELECT name FROM sample WHERE sample_id='smp_old'").fetchone()[0] == "S1"
    assert conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0] == 1

    backups = list((ws / "backups").glob("hte.db.v0-*.bak"))
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])
    assert "parent_run_id" not in {r[1] for r in old.execute("PRAGMA table_info(analysis_run)")}
    old.close()


def test_starting_again_changes_nothing(ws):
    from app.storage import db

    _old_workspace(ws)
    db.init()
    db.close()
    conn = db.init()
    assert len(list((ws / "backups").glob("*.bak"))) == 1        # 没有第二份备份
    n = conn.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0]
    assert n == len(migrations.MIGRATIONS)


def test_a_failing_step_says_where_the_backup_is(ws, monkeypatch):
    from app.storage import db

    _old_workspace(ws)

    def boom(conn):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: sample.name")

    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [
        migrations.Migration(migrations.LATEST + 1, "演示：一步会失败的迁移", "post", boom)])
    monkeypatch.setattr(migrations, "LATEST", migrations.LATEST + 1)
    with pytest.raises(migrations.MigrationError) as e:
        db.init()
    msg = str(e.value)
    assert "演示：一步会失败的迁移" in msg and "backups" in msg and ".bak" in msg
    # 版本号没有被写成最新 —— 下次启动会重试这一步
    conn = sqlite3.connect(ws / "hte.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] < migrations.LATEST
    conn.close()


def test_only_the_newest_backups_are_kept(ws):
    d = ws / "backups"
    d.mkdir()
    for i in range(8):
        (d / f"hte.db.v0-2026010{i}-000000.bak").write_bytes(b"x")
    migrations.prune_backups(keep=5)
    assert len(list(d.glob("*.bak"))) == 5


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="改进程时区要 time.tzset")
def test_step_2_turns_utc_measured_at_into_local_time(ws, monkeypatch):
    """自查 §6 #25：退回文件修改时间的那些原来存成 UTC。升级后和文件夹名里的时间同一种写法。"""
    import time

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        _old_workspace(ws)
        conn = sqlite3.connect(ws / "hte.db")
        conn.executemany(
            "INSERT INTO measurement(measurement_id, sample_id, method, measured_at, created_at)"
            " VALUES(?, 'smp_old', 'spectrum', ?, '2026-08-26T00:00:00+00:00')",
            [("mea_utc", "2026-07-29T10:35:47+00:00"),      # 退回 mtime 的老写法
             ("mea_folder", "2026-07-29T18:25:04.01"),      # 文件夹名里解析的：不动
             ("mea_odd", "看不懂的时间")])                     # 认不出来的：不动
        conn.commit()
        conn.close()

        from app.storage import db
        db.init()
        got = {r["measurement_id"]: r["measured_at"]
               for r in db.query("SELECT measurement_id, measured_at FROM measurement")}
        assert got == {"mea_utc": "2026-07-29T18:35:47",
                       "mea_folder": "2026-07-29T18:25:04.01",
                       "mea_odd": "看不懂的时间"}

        migrations._m2_measured_at_local(db.connect())            # 再跑一遍：什么都不变
        again = {r["measurement_id"]: r["measured_at"]
                 for r in db.query("SELECT measurement_id, measured_at FROM measurement")}
        assert again == got
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_step_3_clears_the_pile_of_empty_conversations(ws):
    """原来每打开一次 AI 抽屉就建一个会话。已经攒下的空会话一次清掉；
    有消息的、术语 ⓘ 的线（空的也留着，ⓘ 要复用）不动。"""
    from app.storage import conversations, db

    db.init()                                                # 新库，直接到最新
    for cid, scope in (("conv_empty1", "{}"), ("conv_empty2", '{"mode": "all"}'),
                       ("conv_used", "{}"), ("conv_term", '{"topic": "term:ot"}')):
        with db.tx() as c:
            c.execute("INSERT INTO conversation(conversation_id, title, scope_json, created_at,"
                      " updated_at) VALUES(?, '新对话', ?, '2026-11-01', '2026-11-01')", (cid, scope))
    conversations.add_message("conv_used", "user", "问过一句")
    with db.tx() as c:                                       # 装作还停在第 2 步
        c.execute("DELETE FROM schema_migration WHERE version >= 3")
        c.execute("PRAGMA user_version = 2")
    db.close()

    report = migrations.run(db.connect(), (config.ROOT / "app/storage/schema.sql").read_text(encoding="utf-8"))
    assert 3 in report["applied"] and report["backup"]          # 之后的步骤也会接着跑
    left = {r["conversation_id"] for r in db.query("SELECT conversation_id FROM conversation")}
    assert left == {"conv_used", "conv_term"}
