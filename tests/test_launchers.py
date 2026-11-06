"""run.bat / run.sh / requirements.txt / CI 对「支持哪几个 Python」必须说同一句话。

2026-11 的教训：run.bat 用 py -3 拿到了 runner 上最新的 Python（推断是 3.14），而钉的
numpy / pandas 没有 3.14 的 Windows 预编译包，pip 开始从源码编译，卡死在「正在安装依赖」。
同事从 python.org 下最新版也会是这样。所以版本范围写在四个地方，这里保证它们一致。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _range(text: str, where: str) -> tuple[int, int]:
    m = re.search(r"\(3, (\d+)\) <= sys\.version_info\[:2\] <= \(3, (\d+)\)", text)
    assert m, f"{where} 里没找到版本范围检查"
    return int(m.group(1)), int(m.group(2))


def test_version_range_agrees_everywhere():
    bat = (ROOT / "run.bat").read_text(encoding="utf-8")
    sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    lo, hi = _range(bat, "run.bat")
    assert _range(sh, "run.sh") == (lo, hi)
    assert f"支持 Python 3.{lo} – 3.{hi}" in req

    # 挑 Python 的顺序：从新到旧把范围里的每个版本都试一遍
    want = [f"3.{v}" for v in range(hi, lo - 1, -1)]
    m = re.search(r"for %%v in \(([\d. ]+)\) do", bat)
    assert m and m.group(1).split() == want
    m = re.search(r"for c in ((?:python3\.\d+ )+)python3; do", sh)
    assert m and [c.removeprefix("python") for c in m.group(1).split()] == want

    # CI 在 Windows 上两头都测
    win = set(re.findall(r"os: windows-latest, python: \"(3\.\d+)\"", ci))
    assert {f"3.{lo}", f"3.{hi}"} <= win, win


def test_launchers_never_compile_dependencies():
    """只装预编译包：没有 wheel 时立刻报错，而不是在没有编译器的电脑上卡住。"""
    for name in ("run.bat", "run.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if "pip install -r requirements.txt" in ln
                 and not ln.lstrip().startswith(("echo", "rem", "#"))]
        assert lines, name
        assert all("--only-binary=:all:" in ln for ln in lines), (name, lines)


def test_run_bat_keeps_crlf():
    """cmd 解析 LF 换行的批处理有已知的怪毛病（标签、括号块错位）。.gitattributes 守着检出，这里守着提交。"""
    raw = (ROOT / "run.bat").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "run.bat 必须是 CRLF 换行"


def test_ci_workflow_is_valid_yaml():
    """工作流文件解析不了时，GitHub 一个任务都不跑，只在网页上标红 —— 推上去才知道。
    2026-11 就因为一行 `run: … --only-binary=:all:`（结尾的冒号被当成「键:」）白推了一次。

    PyYAML 还没有 3.15 的预编译包，所以不放进 requirements-dev.txt；装了就查，没装就跳过。
    """
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert {"lint", "test", "startup-windows", "startup-linux"} <= set(wf["jobs"])
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            assert isinstance(step, dict) and ("run" in step or "uses" in step), (name, step)
