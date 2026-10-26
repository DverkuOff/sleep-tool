#!/bin/bash
# Ставит Sleep Tool: окружение Python, агент launchd (автозапуск), приложение в ~/Applications.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Sleep Tool.app"
LABEL="com.chill.sleep-tool"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
cd "$DIR"

echo "→ Окружение Python"
if command -v uv >/dev/null 2>&1; then
  [ -x .venv/bin/python ] || uv venv -q .venv
  uv pip install -q --python .venv/bin/python -r requirements.txt
else
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install -q -r requirements.txt
fi

echo "→ Агент launchd: $PLIST"
# Запуск только через launchd: если стартовать Python из .app, macOS 26 прячет иконку.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
pkill -f "$DIR/sleep_tool.py" 2>/dev/null || true
.venv/bin/python sleep_tool.py --write-agent

echo "→ Приложение: $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sleep Tool</string>
  <key>CFBundleIdentifier</key><string>$LABEL.launcher</string>
  <key>CFBundleExecutable</key><string>sleep-tool</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
cat > "$APP/Contents/MacOS/sleep-tool" <<'LAUNCHER'
#!/bin/bash
# Просит launchd поднять Sleep Tool (если уже запущен, ничего не делает).
D="gui/$(id -u)"; L="com.chill.sleep-tool"
launchctl bootstrap "$D" "$HOME/Library/LaunchAgents/$L.plist" 2>/dev/null
launchctl kickstart "$D/$L"
LAUNCHER
chmod +x "$APP/Contents/MacOS/sleep-tool"

echo "→ Запускаю"
for _ in 1 2 3 4 5; do  # сразу после bootout launchd иногда ещё не готов
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && break
  sleep 1
done
launchctl kickstart "$DOMAIN/$LABEL" 2>/dev/null || true
sleep 3
if pgrep -f "$DIR/sleep_tool.py" >/dev/null; then
  tail -1 "$HOME/Library/Logs/sleep-tool.log"
  echo "Готово. Таблетка в строке меню."
else
  echo "Не запустилось. Лог: ~/Library/Logs/sleep-tool.log"
  exit 1
fi
