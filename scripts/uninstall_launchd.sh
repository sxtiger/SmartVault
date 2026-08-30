#!/bin/bash
# 卸载 SmartVault 的两个 launchd 服务
set -euo pipefail
UID_NUM="$(id -u)"
for LABEL in com.user.aibrain com.user.aibrain.rag; do
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
    echo "✓ 已停止并移除：$LABEL"
done
