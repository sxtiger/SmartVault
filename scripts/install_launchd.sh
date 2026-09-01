#!/bin/bash
# =====================================================================
# SmartVault launchd 服务一键安装（生成 plist 并 bootstrap 到 launchd）
# 用法：  bash scripts/install_launchd.sh
# 自定义 Python：PYTHON=/path/to/python bash scripts/install_launchd.sh
#
# 已知坑（本脚本已处理）：
#  1. bootout 拆除服务是异步的，紧接着 bootstrap 同名标签会报
#     "Bootstrap failed: 5: Input/output error"（旧实例还没拆完）
#     → bootout 后轮询等待作业真正消失，bootstrap 失败自动重试
#  2. plist 若带 com.apple.quarantine 属性会被 launchd 拒绝 → 主动清除
#  3. macOS TCC（Sequoia/26+）：launchd 打不开 ~/Documents 下的 stdout/stderr
#     日志文件（kTCCServiceSystemPolicyDocumentsFolder 拒绝 xpcproxy 的授权请求），
#     作业在 exec 前即以 78 EX_CONFIG 秒退且日志为空（重启后必现）
#     → 日志统一放 ~/Library/Logs/SmartVault（不受 TCC 保护），模板用 @LOG_DIR@ 占位
# =====================================================================
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"
LOG_DIR="$HOME/Library/Logs/SmartVault"
UID_NUM="$(id -u)"
FAILED=0

if [ ! -x "$PYTHON_BIN" ]; then
    echo "✗ 未找到虚拟环境 Python：$PYTHON_BIN"
    echo "  请先执行：/opt/homebrew/bin/python3.12 -m venv \"$PROJECT_DIR/.venv\" 并安装依赖"
    exit 1
fi

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

# 一次性迁移：旧项目内 logs/ 的历史日志复制到新目录（保留原件不动，幂等只补缺）。
# 注意 .menubar_err_state.json 等隐藏状态文件刻意不迁移——换目录即重置增量扫描基线。
for _f in "$PROJECT_DIR/logs"/*.log "$PROJECT_DIR/logs"/*.log.*; do
    [ -f "$_f" ] && [ ! -f "$LOG_DIR/$(basename "$_f")" ] && cp -p "$_f" "$LOG_DIR/"
done

job_exists() {  # 作业是否还在 launchd 域中
    launchctl print "gui/$UID_NUM/$1" >/dev/null 2>&1
}

wait_job_gone() {  # 等待作业从 launchd 域真正消失（最多 ~10s）
    local label="$1" i
    for i in $(seq 1 20); do
        job_exists "$label" || return 0
        sleep 0.5
    done
    return 1
}

install_one() {
    local label="$1" src dst i
    src="$PROJECT_DIR/launchd/$label.plist"
    dst="$HOME/Library/LaunchAgents/$label.plist"
    sed -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
        -e "s|@PYTHON@|$PYTHON_BIN|g" \
        -e "s|@LOG_DIR@|$LOG_DIR|g" "$src" > "$dst"
    chmod 644 "$dst"
    xattr -d com.apple.quarantine "$dst" 2>/dev/null || true

    # 幂等：先拆除旧实例并等待其真正退出
    if job_exists "$label"; then
        launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
        wait_job_gone "$label" || echo "  ⚠ 旧实例拆除缓慢，继续尝试…"
    fi

    # bootstrap（带重试，规避拆除竞态导致的 error 5）
    for i in 1 2 3 4 5; do
        if launchctl bootstrap "gui/$UID_NUM" "$dst" 2>/tmp/aibrain_install_err.$$; then
            echo "✓ 已安装并启动：$label"
            return 0
        fi
        sleep 1
    done
    echo "✗ $label 安装失败："
    sed 's/^/    /' /tmp/aibrain_install_err.$$ 2>/dev/null
    rm -f /tmp/aibrain_install_err.$$
    FAILED=1
    return 1
}

install_one com.user.aibrain
install_one com.user.aibrain.rag

echo ""
echo "当前 launchd 状态："
launchctl list | grep aibrain || echo "（无 aibrain 作业在运行！）"
echo ""
echo "日志：tail -f $LOG_DIR/*.log"
exit $FAILED
