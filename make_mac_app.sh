#!/bin/bash
# 把星期八打包成 Mac 软件：/Applications/星期八.app（双击启动，只在菜单栏显示“八”字图标，不弹终端窗口）
# 用法：在项目文件夹里运行  bash make_mac_app.sh
# App 里只放一个小启动器，真正的程序仍然是这个项目文件夹（改了代码不用重新打包）。
set -eu
PROJECT="$(cd -- "$(dirname -- "$0")" && pwd)"
APP="/Applications/星期八.app"
PY="$PROJECT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "找不到 $PY，请先按 docs/Mac.md 建好 .venv"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 启动器：用 exec 让 Python 直接接替这个进程，macOS 才会把麦克风权限算在“星期八”名下
cat > "$APP/Contents/MacOS/xingqiba" <<EOF
#!/bin/bash
cd "$PROJECT" || exit 1
mkdir -p logs
exec "$PY" "$PROJECT/app.py" >>"$PROJECT/logs/mac-start.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/xingqiba"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>星期八</string>
  <key>CFBundleDisplayName</key><string>星期八</string>
  <key>CFBundleIdentifier</key><string>local.xingqiba.assistant</string>
  <key>CFBundleExecutable</key><string>xingqiba</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>星期八要听你说话：唤醒词、语音指令和对话都需要麦克风。</string>
  <key>NSAppleEventsUsageDescription</key><string>星期八用它调节系统音量。</string>
</dict>
</plist>
EOF

# 图标：用星期八自己的“八”字图标，画成各种尺寸
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
"$PY" - "$ICONSET" <<'EOF'
import sys
sys.path.insert(0, ".")
import appicon
out = sys.argv[1]
for size in (16, 32, 128, 256, 512):
    appicon.make_image(size=size).save(f"{out}/icon_{size}x{size}.png")
    appicon.make_image(size=size * 2).save(f"{out}/icon_{size}x{size}@2x.png")
EOF
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

# 本机签名（不是开发者证书），让 macOS 记住麦克风授权
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
echo "已生成：$APP"
