"""智能体跑命令（P-52：「智能体需要跑命令！」）—— pi-agent 那种 bash 工具，加上这个平台的几道闸。

  · 工作目录默认是智能体专用目录 `workspace/agent/`；指定别的目录也必须是**可写**的文件夹（不能在平台核心里跑）
  · PATH 最前面是平台自己的 Python —— `python 脚本.py` 用的就是平台那套 numpy / pandas
  · 超时（默认 60 秒、最多 600 秒）和「停止」都会把**整棵进程树**杀掉，不留后台进程
  · 输出按 UTF-8 解，不行就按系统编码（中文 Windows 是 GBK）；先打码再给模型；太长只留头尾
  · 一眼就能看出会毁东西的命令（rm -rf、del /s、format、shutdown、curl | sh……）**每次都问**，自动模式也不例外
  · **核心守卫**：跑之前、跑之后各给平台核心（安装目录里、工作区以外的文件）拍一张快照，
    有任何文件被改 / 加 / 删，结果里大字写出来 —— 命令是以你的身份跑的，平台挡不住它碰哪儿，但改了一定说出来

**这不是沙箱**：命令能做的和你在终端里能做的一样多。所以缺省是「每次问我」，自动模式要你自己打开。
"""
from __future__ import annotations

import locale
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from app import config
from app.agent import files

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
OUTPUT_KEEP = 12000           # 给模型的输出最多这么多字（头 3000 + 尾 9000）
OUTPUT_CAP = 2_000_000        # 进程吐得再多，内存里最多攒这么多字节

# 一眼能看出会毁东西的 —— 这类每次都问（风险升成 irreversible）
DANGEROUS = [
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+", "rm -r / -f：递归或强制删除"),
    (r"\b(del|erase)\s+.*(/s|/q)", "del /s /q：递归删除"),
    (r"\b(rmdir|rd)\s+.*/s", "rmdir /s：删整个目录"),
    (r"Remove-Item\b.*-Recurse", "Remove-Item -Recurse：递归删除"),
    (r"\b(mkfs|format)\b", "格式化磁盘"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机 / 重启"),
    (r"\bdd\s+if=", "dd：直接写磁盘"),
    (r"(curl|wget|iwr|Invoke-WebRequest)\b.*\|\s*(sh|bash|python|iex|powershell)", "下载了直接执行"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f)", "git 推送 / 硬重置 / 清理"),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b", "递归改权限"),
    (r"\bpip\s+(install|uninstall)\b|\bnpm\s+(install|i|uninstall)\s+-g\b", "装 / 卸全局依赖（会改平台的运行环境）"),
    (r"(hte\.db|providers\.json)", "直接碰平台的数据库 / 密钥文件"),
]


def danger_of(command: str) -> str | None:
    for pat, why in DANGEROUS:
        if re.search(pat, command or "", re.I):
            return why
    return None


# ------------------------------------------------------------------ 核心快照
_SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"}


def core_snapshot() -> dict[str, tuple[int, int]]:
    """平台核心（安装目录里、工作区以外）每个文件的 (大小, 修改时间)。几百个文件，几毫秒。"""
    root = files._real(config.ROOT)
    ws = files._real(config.WORKSPACE)
    snap: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [x for x in dirnames if x not in _SKIP and not files._within(files._real(d / x), ws)]
        for name in filenames:
            f = d / name
            try:
                st = f.stat()
            except OSError:
                continue
            snap[str(f.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return snap


def core_diff(before: dict, after: dict) -> list[str]:
    out = [f"改了 {k}" for k in sorted(before) if k in after and before[k] != after[k]]
    out += [f"新增 {k}" for k in sorted(after) if k not in before]
    out += [f"删了 {k}" for k in sorted(before) if k not in after]
    return out


# ------------------------------------------------------------------ 跑
def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def _clip(text: str) -> str:
    if len(text) <= OUTPUT_KEEP:
        return text
    head, tail = text[:3000], text[-(OUTPUT_KEEP - 3000):]
    return f"{head}\n…（中间省略 {len(text) - OUTPUT_KEEP} 字）…\n{tail}"


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run(command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT,
        should_stop: Callable[[], bool] = lambda: False) -> dict:
    command = (command or "").strip()
    if not command:
        raise files.FileToolError("命令是空的")
    workdir = files.resolve(cwd, write=True) if cwd else files.agent_dir()
    if not workdir.is_dir():
        raise files.FileToolError(f"工作目录不存在：{cwd}")
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["HTE_AGENT_DIR"] = str(files.agent_dir())
    env["HTE_WORKSPACE"] = str(config.WORKSPACE)

    before = core_snapshot()
    t0 = time.monotonic()
    kw: dict = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    proc = subprocess.Popen(command, shell=True, cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    buf = bytearray()

    def pump() -> None:
        for chunk in iter(lambda: proc.stdout.read(4096), b""):
            if len(buf) < OUTPUT_CAP:
                buf.extend(chunk[: OUTPUT_CAP - len(buf)])

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = stopped = False
    while proc.poll() is None:
        if time.monotonic() - t0 > timeout:
            timed_out = True
        elif should_stop():
            stopped = True
        if timed_out or stopped:
            _kill_tree(proc)
            break
        time.sleep(0.1)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
    reader.join(timeout=2)
    ms = round((time.monotonic() - t0) * 1000)
    changed = core_diff(before, core_snapshot())
    out = {
        "command": command, "cwd": files.shown(workdir), "exit_code": proc.returncode, "ms": ms,
        "output": _clip(files.redact(_decode(bytes(buf)))),
    }
    if timed_out:
        out["note"] = f"超过 {timeout} 秒，整棵进程树已经杀掉；要更久把 timeout 调大（最多 {MAX_TIMEOUT}）"
    if stopped:
        out["note"] = "用户点了停止，命令已经杀掉"
    if changed:
        out["core_changed"] = changed[:50]
        out["warning"] = "⚠ 平台核心的文件被这条命令改动了 —— 网站的核心不应该变。如实告诉用户改了哪些，不要再碰"
    return out


def preview(command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT) -> str:
    try:
        workdir = files.shown(files.resolve(cwd, write=True)) if cwd else f"专用目录 {files.AGENT_DIR}/"
    except files.FileToolError as exc:
        return f"跑命令：{command}\n（工作目录不行，批了也会失败：{exc}）"
    t = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    why = danger_of(command)
    lines = [f"在{workdir} 里跑（最多 {t} 秒）：" if workdir.startswith("专用") else f"在 {workdir} 里跑（最多 {t} 秒）：", f"$ {command}"]
    if why:
        lines.append(f"⚠ 这条命令看起来会{why} —— 每次都要你点头")
    lines.append("命令以你的身份运行，能做的和你在终端里一样多；跑完会检查平台核心有没有被改。")
    return "\n".join(lines)
