#!/usr/bin/env bash
# HTE Studio —— macOS / Linux 开发入口。Windows 用 run.bat。
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
  echo "需要 Python 3.11+（当前：$("$PY" --version 2>&1)）" >&2
  exit 1
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
  "$VPY" -m pip install -r requirements.txt --quiet --disable-pip-version-check
  echo "$REQHASH" > "$STAMP"
else
  echo "  [2/3] 依赖就绪"
fi

echo "  [3/3] 正在启动..."
exec "$VPY" -m app.main "$@"
