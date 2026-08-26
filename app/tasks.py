"""后台任务队列。

1000 个样品 × 约 1 秒 = 17 分钟，同步请求必然超时。所以批处理走这里：
提交后立刻返回 task_id，前端轮询进度。

单机单用户，不需要 Celery/Redis 那一套 —— 一个线程池 + 一张表就够，
而且任务状态落在 SQLite 里，服务重启后还能看到上次跑到哪儿了
（重启时正在跑的会被标成 interrupted，不会永远挂在 running）。
"""
from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from app.storage import db

# 任务种类 → 执行函数。函数签名：fn(ctx) -> dict
_HANDLERS: dict[str, Callable[["TaskContext"], dict]] = {}

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_cancelled: set[str] = set()


class Cancelled(Exception):
    """任务被取消。不是错误，不该记成 failed。"""


@dataclass
class TaskContext:
    """执行函数拿到的东西。"""
    task_id: str
    params: dict

    def progress(self, done: int, total: int, message: str = "") -> None:
        """报告进度。同时检查有没有被取消 —— 取消是协作式的。"""
        self.check_cancelled()
        with db.tx() as c:
            c.execute("UPDATE task SET progress=?, total=?, message=? WHERE task_id=?",
                      (int(done), int(total), message or None, self.task_id))

    def tally(self, n_ok: int, n_failed: int) -> None:
        with db.tx() as c:
            c.execute("UPDATE task SET n_ok=?, n_failed=? WHERE task_id=?",
                      (int(n_ok), int(n_failed), self.task_id))

    def check_cancelled(self) -> None:
        if self.task_id in _cancelled:
            raise Cancelled()

    @property
    def is_cancelled(self) -> bool:
        return self.task_id in _cancelled


def register(kind: str) -> Callable:
    """装饰器：把一个函数注册成某种任务的执行体。"""
    def deco(fn: Callable[[TaskContext], dict]) -> Callable:
        _HANDLERS[kind] = fn
        return fn
    return deco


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            # 任务之间不并行 —— 一次跑一个批处理，进度才好读，
            # 也避免几个批处理同时抢 CPU 反而都变慢。任务内部另有并行。
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hte-task")
        return _executor


def submit(kind: str, params: dict | None = None, title: str = "") -> dict:
    if kind not in _HANDLERS:
        raise KeyError(f"没有注册这种任务：{kind}")

    task_id = db.new_id("task")
    with db.tx() as c:
        c.execute(
            "INSERT INTO task(task_id, kind, title, params_json, status, created_at)"
            " VALUES(?,?,?,?, 'queued', ?)",
            (task_id, kind, title or kind, json.dumps(params or {}, ensure_ascii=False),
             db.now()))

    _pool().submit(_run, task_id, kind, params or {})
    return get(task_id)


def _run(task_id: str, kind: str, params: dict) -> None:
    with db.tx() as c:
        c.execute("UPDATE task SET status='running', started_at=? WHERE task_id=?",
                  (db.now(), task_id))
    ctx = TaskContext(task_id=task_id, params=params)
    try:
        result = _HANDLERS[kind](ctx) or {}
        status, error = "ok", None
    except Cancelled:
        result, status, error = {}, "cancelled", None
    except Exception as exc:
        result, status = {}, "failed"
        error = f"{exc}\n\n{traceback.format_exc()[-4000:]}"
    finally:
        _cancelled.discard(task_id)

    with db.tx() as c:
        c.execute("UPDATE task SET status=?, error=?, result_json=?, finished_at=?"
                  " WHERE task_id=?",
                  (status, error, json.dumps(result, ensure_ascii=False, default=str),
                   db.now(), task_id))


def cancel(task_id: str) -> dict:
    """协作式取消：置个标志，任务在下一次 progress() 时抛出。

    已经跑完的样品不会回滚 —— 取消是「停在这儿」，不是「当没发生过」。
    """
    row = db.query_one("SELECT status FROM task WHERE task_id=?", (task_id,))
    if not row:
        raise KeyError(f"没有这个任务：{task_id}")
    if row["status"] in ("ok", "failed", "cancelled"):
        return get(task_id)
    _cancelled.add(task_id)
    with db.tx() as c:
        c.execute("UPDATE task SET message=? WHERE task_id=?", ("正在停止…", task_id))
    return get(task_id)


def get(task_id: str) -> dict:
    row = db.query_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    if not row:
        raise KeyError(f"没有这个任务：{task_id}")
    return _hydrate(row)


def _hydrate(row: dict) -> dict:
    for key, target in (("params_json", "params"), ("result_json", "result")):
        try:
            row[target] = json.loads(row.get(key) or "{}")
        except json.JSONDecodeError:
            row[target] = {}
    total = row.get("total") or 0
    row["percent"] = round(100 * (row.get("progress") or 0) / total, 1) if total else None
    row["done"] = row["status"] in ("ok", "failed", "cancelled")
    return row


def recent(limit: int = 20) -> list[dict]:
    return [_hydrate(r) for r in db.query(
        "SELECT * FROM task ORDER BY created_at DESC LIMIT ?", (limit,))]


def reap_interrupted() -> int:
    """启动时把上次没跑完的标掉。

    不这么做的话，重启后会有任务永远显示 running —— 用户会一直等一个
    早就死了的东西。
    """
    rows = db.query("SELECT task_id FROM task WHERE status IN ('queued','running')")
    if not rows:
        return 0
    with db.tx() as c:
        c.execute("UPDATE task SET status='failed', error=?, finished_at=?"
                  " WHERE status IN ('queued','running')",
                  ("服务重启，任务被中断。已经跑完的样品结果保留，重新提交会跳过它们。",
                   db.now()))
    return len(rows)


def shutdown(wait: bool = False) -> None:
    global _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=True)
            _executor = None
