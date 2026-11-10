"""原子写：先写同目录下的临时文件，再 os.replace 换上去。

为什么要同目录：os.replace 只有在同一个文件系统里才是原子的；临时文件名带进程号和随机串，
两个线程同时写同一个目标也不会互相踩。出错时临时文件一定删掉，不留垃圾。
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable


def _tmp_for(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")


def atomic_write_with(target: str | Path, write: Callable[[Path], None]) -> Path:
    """`write(tmp_path)` 把内容写进临时文件；写完了才换上去。给 np.savez、shutil.copy 这种自己开文件的用。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(target)
    try:
        write(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def atomic_write_bytes(target: str | Path, data: bytes) -> Path:
    return atomic_write_with(target, lambda tmp: tmp.write_bytes(data))


def atomic_write_text(target: str | Path, text: str, encoding: str = "utf-8") -> Path:
    # newline="" 不让 Windows 把 \n 偷偷换成 \r\n —— 写出去的就是给的那串字
    def write(tmp: Path) -> None:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
    return atomic_write_with(target, write)


def atomic_write_json(target: str | Path, obj: Any, *, indent: int | None = 1, default=None) -> Path:
    return atomic_write_text(target, json.dumps(obj, ensure_ascii=False, indent=indent, default=default))
