"""SQLite 连接、建表与设置读写。

用原生 sqlite3 而不是 ORM：schema 只有 8 张表，显式 SQL 更容易读、
也更容易在需要时整体迁到 PostgreSQL。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app import config

_local = threading.local()
_SCHEMA = Path(__file__).with_name("schema.sql")


def now() -> str:
    """统一用 UTC ISO8601，避免跨时区/夏令时的坑。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """拿到当前线程的连接。FastAPI 的线程池会复用线程，所以按线程缓存。"""
    db_path = Path(path) if path else config.DB_PATH
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "path", None) == str(db_path):
        return cached

    if cached is not None:
        cached.close()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # 读写并发，界面查询不被写入阻塞
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    _local.conn = conn
    _local.path = str(db_path)
    return conn


def close() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


def init(path: Path | None = None) -> sqlite3.Connection:
    """建表。幂等，每次启动都跑。"""
    conn = connect(path)
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


@contextmanager
def tx(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """一个写事务。异常时回滚，不留半截数据。"""
    c = conn or connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


# ------------------------------------------------------------------ 查询助手
def query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    row = connect().execute(sql, params).fetchone()
    return dict(row) if row else None


def scalar(sql: str, params: tuple | dict = ()) -> Any:
    row = connect().execute(sql, params).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------ 设置
def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value_json FROM app_setting WHERE key = ?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except json.JSONDecodeError:
        return default


def set_setting(key: str, value: Any) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO app_setting(key, value_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now()),
        )


def all_settings() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in query("SELECT key, value_json FROM app_setting"):
        try:
            out[row["key"]] = json.loads(row["value_json"])
        except json.JSONDecodeError:
            continue
    return out


def seed_defaults() -> None:
    """把 config.DEFAULTS 里还没写进库的键补上。已有的不覆盖，尊重用户改动。"""
    d = config.DEFAULTS
    for key, value in (
        ("copy_extensions", list(d.copy_extensions)),
        ("reference_extensions", list(d.reference_extensions)),
        ("unknown_policy", d.unknown_policy),
        ("naming_rules", list(d.naming_rules)),
        ("thumbnail_max_px", d.thumbnail_max_px),
        ("max_preview_rows", d.max_preview_rows),
        ("active_provider", None),
        ("active_model", None),
    ):
        if query_one("SELECT 1 FROM app_setting WHERE key = ?", (key,)) is None:
            set_setting(key, value)
