#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"

if [[ ! -x .venv/bin/python ]]; then
    printf '%s\n' '找不到 Mac 的 Python 虚拟环境。请按 docs/Mac.md 创建 .venv。' >&2
    exit 1
fi

mkdir -p logs
nohup "$PWD/.venv/bin/python" "$PWD/app.py" </dev/null >>logs/mac-start.log 2>&1 &
printf '%s\n' '已发起启动。请查看菜单栏的「八」字图标。启动日志在 logs/mac-start.log 和 logs/errors.log。'
