#!/bin/bash
# =============================================================
# 生成 SmartVaultMenuBar.app（菜单栏控制台启动器，不入 Dock）
# 用法：bash scripts/build_menubar_app.sh
#   可用 PYTHON=... 覆盖默认解释器（.venv/bin/python）
# =============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"
APP="$PROJECT_DIR/SmartVaultMenuBar.app"

[ -x "$PYTHON_BIN" ] || { echo "✗ 未找到 Python：$PYTHON_BIN（先创建 .venv）"; exit 1; }
"$PYTHON_BIN" -c "import rumps" 2>/dev/null || {
    echo "✗ 缺少 rumps，请先：$PYTHON_BIN -m pip install rumps"; exit 1;
}

mkdir -p "$APP/Contents/MacOS"

# 启动器脚本：以 launchd 用户代理方式拉起菜单栏控制台。
# 不能直接 exec python —— 经 Finder/open 启动的 GUI app 受 macOS TCC 限制，
# 无法读取 ~/Documents 下的项目文件（pyvenv.cfg 都读不了）；
# 而 launchd 启动的用户代理无此限制（ingest/rag 服务即实证）。
# 因此 plist 内容在构建时硬编码，启动器运行时不读任何项目内文件。
cat > "$APP/Contents/MacOS/SmartVaultMenuBar" <<LAUNCHER_EOF
#!/bin/bash
# SmartVaultMenuBar 启动器：确保 com.user.aibrain.menubar launchd 代理运行
LABEL="com.user.aibrain.menubar"
DOMAIN="gui/\$(id -u)"
PLIST="\$HOME/Library/LaunchAgents/\$LABEL.plist"

mkdir -p "\$HOME/Library/LaunchAgents"
cat > "\$PLIST" <<'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.aibrain.menubar</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>-u</string>
        <string>$PROJECT_DIR/menu_bar_app.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/menubar.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/menubar.stderr.log</string>
</dict>
</plist>
PLISTEOF

if launchctl print "\$DOMAIN/\$LABEL" >/dev/null 2>&1; then
    # 已安装（运行中或已退出）：重启一次，确保菜单栏出现
    launchctl kickstart -k "\$DOMAIN/\$LABEL"
else
    i=0
    until launchctl bootstrap "\$DOMAIN" "\$PLIST" 2>/dev/null; do
        i=\$((i+1))
        [ \$i -ge 5 ] && osascript -e 'display alert "SmartVault" message "launchctl bootstrap 失败，请查看 logs/menubar.stderr.log"' && exit 1
        sleep 1
    done
fi
exit 0
LAUNCHER_EOF
chmod 755 "$APP/Contents/MacOS/SmartVaultMenuBar"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleExecutable</key>
    <string>SmartVaultMenuBar</string>
    <key>CFBundleIdentifier</key>
    <string>com.user.aibrain.menubar</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>SmartVaultMenuBar</string>
    <key>CFBundleDisplayName</key>
    <string>SmartVault 菜单栏</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

# ad-hoc 签名（本地生成无隔离属性，签名仅为更稳妥）
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "✓ 已生成 $APP"
echo "  启动：双击或 open \"$APP\"（= 安装/唤醒 launchd 代理 com.user.aibrain.menubar，"
echo "        同时注册开机自启；终端调试可直跑 .venv/bin/python menu_bar_app.py）"
echo "  注：经 Finder 启动的 GUI app 受 macOS TCC 限制读不了 ~/Documents，"
echo "      故本 app 只是启动器，菜单栏进程由 launchd 拉起（与 ingest/rag 同机制）。"
