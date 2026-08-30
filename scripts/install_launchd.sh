#!/bin/bash
# =====================================================================
# SmartVault launchd 服务一键安装（生成 plist 并 bootstrap 到 launchd）
# 用法：  bash scripts/install_launchd.sh
# 自定义 Python：PYTHON=/path/to/python bash scripts/install_launchd.sh
# =====================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"
UID_NUM="$(id -u)"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "✗ 未找到虚拟环境 Python：$PYTHON_BIN"
    echo "  请先执行：/opt/homebrew/bin/python3.12 -m venv \"$PROJECT_DIR/.venv\" 并安装依赖"
    exit 1
fi

mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"

for LABEL in com.user.aibrain com.user.aibrain.rag; do
    SRC="$PROJECT_DIR/launchd/$LABEL.plist"
    DST="$HOME/Library/LaunchAgents/$LABEL.plist"
    sed -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
        -e "s|@PYTHON@|$PYTHON_BIN|g" "$SRC" > "$DST"
    # 幂等：先卸载旧实例再安装
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$DST"
    echo "✓ 已安装并启动：$LABEL -> $DST"
done

echo ""
echo "日志：tail -f $PROJECT_DIR/logs/*.log"
echo "状态：launchctl list | grep aibrain"
