#!/usr/bin/env bash
# HTE Studio —— macOS / Linux 开发入口。Windows 用 run.bat。
set -euo pipefail
cd "$(dirname "$0")"

# 支持 3.11 到 3.15（依赖只在这几个版本上都有预编译包，见 requirements.txt 开头）。
# 没指定 PYTHON 时从新到旧挑第一个装了的。改范围要连 run.bat、CI 一起改。
PYOK='import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 15) else 1)'
if [ -z "${PYTHON:-}" ]; then
  for c in python3.15 python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "$PYOK" 2>/dev/null; then PYTHON=$c; break; fi
  done
fi
PY=${PYTHON:-python3}
if ! "$PY" -c "$PYOK" 2>/dev/null; then
  echo "需要 Python 3.11 到 3.15（当前：$("$PY" --version 2>&1)）。可以用 PYTHON=python3.13 ./run.sh 指定" >&2
  exit 1
fi

# 以前用不支持的 Python 建的虚拟环境：重建
if [ -x ".venv/bin/python" ] && ! .venv/bin/python -c "$PYOK" 2>/dev/null; then
  echo "  [!] 虚拟环境的 Python 版本不在支持范围内，正在重建..."
  rm -rf .venv
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "  [1/3] 首次运行，正在创建虚拟环境..."
  "$PY" -m venv .venv
else
  echo "  [1/3] 虚拟环境就绪"
fi
VPY=".venv/bin/python"

STAMP=".venv/.deps-hash"
REQHASH=$("$PY" -c "import hashlib;print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest()[:16])")
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$REQHASH" ]; then
  echo "  [2/3] 正在安装依赖..."
  "$VPY" -m pip install --upgrade pip --quiet --disable-pip-version-check
  "$VPY" -m pip install -r requirements.txt --only-binary=:all: --quiet --disable-pip-version-check
  echo "$REQHASH" > "$STAMP"
else
  echo "  [2/3] 依赖就绪"
fi

echo "  [3/3] 正在启动..."
exec "$VPY" -m app.main "$@"
