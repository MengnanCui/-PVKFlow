"""数据库版本与迁移。

原来没有版本号：每次启动重跑一遍 schema.sql（全是 IF NOT EXISTS），新列靠
`_add_missing_columns` 补。这条路只够「加表、加列、加索引」，不够：
  - 改约束、回填数据、拆开早期误合并的样品 —— 没有地方写
  - 以后加一条唯一索引，碰上旧数据里的重复 —— 启动直接失败，而且不知道是第几步坏的

现在：
  - 版本号存在 SQLite 自带的 `PRAGMA user_version` 里；每一步迁移另记一行 `schema_migration`
  - 已有数据的工作区**升级之前先备份** hte.db（`workspace/backups/`），迁移失败时报错里写着备份在哪
  - 全新的工作区直接按 schema.sql 建到最新，不走迁移
  - 迁移分两段：`pre`（在 schema.sql 之前跑：只做补列 —— 脚本里的索引要用到新列）
    和 `post`（在 schema.sql 之后跑：表都在了，可以搬数据）

加一步迁移：在 MIGRATIONS 末尾追加一条，版本号 +1。**已经发出去的迁移不要改**——
用户的库已经按它升级过了。

**每一步都必须能重跑。** `executescript` 会先隐式提交，所以中途失败时库可能停在
「前几步做完了、版本号还是旧的」—— 下次启动会把这些步骤再跑一遍。
补列前先查列在不在、搬数据前先查是不是已经搬过，就是这个原因。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from app import config


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    phase: str                                  # "pre" | "post"
    apply: Callable[[sqlite3.Connection], None]


# ---------------------------------------------------------------- 各步迁移
def _m1_add_columns(conn: sqlite3.Connection) -> None:
    """2026-08 加的两列。原来在 db._add_missing_columns 里，每次启动都检查一遍。"""
    wanted = {"analysis_run": [("parent_run_id", "TEXT")],
              "artifact": [("is_matrix", "INTEGER")]}
    for table, cols in wanted.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue                            # 表还没有：schema.sql 建表时自带这些列
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


MIGRATIONS: list[Migration] = [
    Migration(1, "补列：analysis_run.parent_run_id、artifact.is_matrix", "pre", _m1_add_columns),
]

LATEST = max(m.version for m in MIGRATIONS)


# ---------------------------------------------------------------- 驱动
def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _is_fresh(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sample'"
    ).fetchone()[0] == 0


def backup(conn: sqlite3.Connection, from_version: int) -> Path:
    """用 SQLite 自己的在线备份接口拷一份（WAL 模式下直接复制文件可能拷到半截）。"""
    d = config.WORKSPACE / "backups"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = d / f"hte.db.v{from_version}-{stamp}.bak"
    with sqlite3.connect(dest) as out:
        conn.backup(out)
    return dest


class MigrationError(RuntimeError):
    pass


def run(conn: sqlite3.Connection, schema_sql: str) -> dict:
    """建表 + 迁移到最新。幂等：已经是最新的库只跑一遍 IF NOT EXISTS 的建表脚本。"""
    _ensure_history_table(conn)
    fresh = _is_fresh(conn)
    before = current_version(conn)
    todo = [] if fresh else [m for m in MIGRATIONS if m.version > before]
    backup_path = backup(conn, before) if todo else None

    def fail(step: str, exc: Exception) -> MigrationError:
        where = f"升级前的备份在 {backup_path}" if backup_path else "这一步之前没有改动数据"
        return MigrationError(f"数据库升级失败（{step}）：{exc}。{where}。"
                              "把这段报错发给维护者；平台没有改动你的原始数据文件。")

    for m in todo:
        if m.phase == "pre":
            try:
                m.apply(conn)
            except Exception as exc:                   # noqa: BLE001
                conn.rollback()
                raise fail(f"第 {m.version} 步：{m.description}", exc) from exc
    try:
        conn.executescript(schema_sql)
    except sqlite3.Error as exc:
        raise fail("建表脚本 schema.sql", exc) from exc
    for m in todo:
        if m.phase == "post":
            try:
                m.apply(conn)
            except Exception as exc:                   # noqa: BLE001
                conn.rollback()
                raise fail(f"第 {m.version} 步：{m.description}", exc) from exc

    now = datetime.now().isoformat(timespec="seconds")
    if fresh:
        conn.execute("INSERT INTO schema_migration(version, description, applied_at)"
                     " VALUES(?,?,?)", (LATEST, f"新建工作区，直接建到第 {LATEST} 版", now))
    for m in todo:
        conn.execute("INSERT INTO schema_migration(version, description, applied_at)"
                     " VALUES(?,?,?)", (m.version, m.description, now))
    conn.execute(f"PRAGMA user_version = {LATEST}")
    conn.commit()
    return {"from": before, "to": LATEST, "fresh": fresh,
            "applied": [m.version for m in todo],
            "backup": str(backup_path) if backup_path else None}


def _ensure_history_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migration ("
                 " version INTEGER NOT NULL, description TEXT NOT NULL,"
                 " applied_at TEXT NOT NULL)")


def prune_backups(keep: int = 5) -> None:
    """备份只留最近几份。升级很少发生，但不该无限堆。"""
    d = config.WORKSPACE / "backups"
    if not d.is_dir():
        return
    olds = sorted(d.glob("hte.db.v*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in olds[keep:]:
        try:
            p.unlink()
        except OSError:
            pass

