#!/bin/bash
# 맥미니 자동 확인(5분마다 hub.py watch) 설치·제거·상태.
#   scripts/install-watcher.sh install     설치하고 바로 시작
#   scripts/install-watcher.sh status      동작 상태
#   scripts/install-watcher.sh uninstall   멈추고 제거
# 이 서비스 하나(kr.personal.agent-hub-watch)만 만들고, 다른 자동 실행 항목은 건드리지 않는다.
set -euo pipefail
HUB="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="kr.personal.agent-hub-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PY="$(command -v python3)"

case "${1:-status}" in
  install)
    mkdir -p "$HUB/logs" "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HUB/scripts/hub.py</string>
    <string>watch</string>
  </array>
  <key>WorkingDirectory</key><string>$HUB</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HUB/logs/watch.log</string>
  <key>StandardErrorPath</key><string>$HUB/logs/watch.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.npm-global/bin:$HOME/.local/bin</string>
    <key>HOME</key><string>$HOME</string>
    <key>PYTHONUTF8</key><string>1</string>
    <key>LANG</key><string>ko_KR.UTF-8</string>
  </dict>
</dict>
</plist>
EOF
    launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "설치됨: $LABEL (5분마다 확인). 로그: $HUB/logs/watch.log"
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" | grep -E "state =|last exit code|run interval" || true
    else
      echo "설치 안 됨"
    fi
    ;;
  uninstall)
    launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "제거됨: $LABEL"
    ;;
  *)
    sed -n '2,5p' "$0"
    ;;
esac
