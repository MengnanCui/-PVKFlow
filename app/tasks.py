"""后台任务队列。

1000 个样品 × 约 1 秒 = 17 分钟，同步请求必然超时。所以批处理走这里：
提交后立刻返回 task_id，前端轮询进度。

单机单用户，不需要 Celery/Redis 那一套 —— 一个线程池 + 一张表就够，
而且任务状态落在 SQLite 里，服务重启后还能看到上次跑到哪儿了
（重启时正在跑的会被标成 failed 并写明「服务重启，任务被中断」，不会永远挂在 running）。
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from app.errors import NotFound
from app.storage import db

# 任务种类 → 执行函数。函数签名：fn(ctx) -> dict
log = logging.getLogger("hte.tasks")

_HANDLERS: dict[str, Callable[["TaskContext"], dict]] = {}

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_cancelled: set[str] = set()


class Cancelled(Exception):
    """任务被取消。不是错误，不该记成 failed。

    可以带上「取消时已经做完的那部分」的摘要 —— 批处理取消后，
    已跑完的样品照样有一次对比可看，前端得知道它的 parent_run_id。
    """

    def __init__(self, result: dict | None = None) -> None:
        super().__init__("cancelled")
        self.result = result or {}


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


# 任务种类 → 服务重启时这种任务被打断后的收尾（参数是这个任务提交时的 params）
_ON_INTERRUPTED: dict[str, Callable[[dict], None]] = {}


def register(kind: str, *, on_interrupted: Callable[[dict], None] | None = None) -> Callable:
    """装饰器：把一个函数注册成某种任务的执行体。

    on_interrupted：服务重启时，这种任务上次没跑完，除了把任务标成失败，还要收拾什么
    （批处理：把它的分析运行标成失败；QFLS：把输出目录的 run.json 标成失败）。
    每种任务自己管自己的收尾，任务队列不需要知道任何一种任务的细节（C074）。
    """
    def deco(fn: Callable[[TaskContext], dict]) -> Callable:
        _HANDLERS[kind] = fn
        if on_interrupted:
            _ON_INTERRUPTED[kind] = on_interrupted
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
    except Cancelled as exc:
        result, status, error = exc.result, "cancelled", None
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
        raise NotFound(f"没有这个任务：{task_id}")
    if row["status"] in ("ok", "failed", "cancelled"):
        return get(task_id)
    _cancelled.add(task_id)
    with db.tx() as c:
        c.execute("UPDATE task SET message=? WHERE task_id=?", ("正在停止…", task_id))
    return get(task_id)


def get(task_id: str) -> dict:
    row = db.query_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    if not row:
        raise NotFound(f"没有这个任务：{task_id}")
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
    rows = db.query("SELECT task_id, kind, params_json FROM task WHERE status IN ('queued','running')")
    if rows:
        with db.tx() as c:
            # 原来这句话还说「重新提交会跳过它们」—— 代码里从来没有跳过的逻辑，
            # 重新提交会把整批重新算一遍。说了做不到的事比不说更糟。
            c.execute("UPDATE task SET status='failed', error=?, finished_at=?"
                      " WHERE status IN ('queued','running')",
                      ("服务重启，任务被中断。已经做完的部分保留；重新提交会从头再做一遍。", db.now()))
    # 每种任务自己的收尾（注册时给的 on_interrupted）。一种任务收尾出错不能挡住别的
    for r in rows:
        hook = _ON_INTERRUPTED.get(r["kind"])
        if not hook:
            continue
        try:
            hook(json.loads(r.get("params_json") or "{}"))
        except Exception:                       # noqa: BLE001
            log.exception("任务 %s（%s）的中断收尾出错", r["task_id"], r["kind"])
    return len(rows)


def shutdown(wait: bool = False) -> None:
    global _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=True)
            _executor = None
