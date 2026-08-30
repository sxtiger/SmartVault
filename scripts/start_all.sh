#!/bin/bash
# =====================================================================
# 前台手动测试：同时启动 RAG 服务与摄入守护进程（Ctrl+C 一起退出）
# 用法：bash scripts/start_all.sh
# =====================================================================
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"

"$PYTHON_BIN" "$PROJECT_DIR/rag_api.py" --config "$PROJECT_DIR/config.json" &
RAG_PID=$!
trap 'kill $RAG_PID 2>/dev/null || true' EXIT

"$PYTHON_BIN" "$PROJECT_DIR/ingest_daemon.py" --config "$PROJECT_DIR/config.json"
