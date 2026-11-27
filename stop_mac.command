#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"

if [[ ! -x .venv/bin/python ]]; then
    printf '%s\n' '找不到 Mac 的 Python 虚拟环境。请按 docs/Mac.md 检查 .venv。' >&2
    exit 1
fi

exec "$PWD/.venv/bin/python" "$PWD/stop.py" --quiet
