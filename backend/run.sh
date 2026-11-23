#!/usr/bin/env bash
# 启动计量复判班报（仅需 Python 3.11，无第三方依赖）。
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"
exec python3 -m src.main
