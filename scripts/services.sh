#!/bin/bash
# 맥미니 상시 서비스 2개를 설치·확인·제거한다. 다른 자동 실행 항목은 건드리지 않는다.
#   kr.personal.agent-hub-watch  5분마다 실행요청 작업 처리(hub.py watch)
#   kr.personal.agent-hub-web    웹 창구(Tailscale 주소에서만, 포트 8787)
#
#   scripts/services.sh install     설치하고 시작 (먼저 python3 web/server.py passwd 로 비밀번호 설정)
#   scripts/services.sh status      상태와 접속 주소
#   scripts/services.sh restart     다시 시작(코드 받은 뒤)
#   scripts/services.sh uninstall   멈추고 제거
set -euo pipefail
HUB="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="gui/$(id -u)"
PY="$(command -v python3)"
AGENTS="$HOME/Library/LaunchAgents"
WATCH=kr.personal.agent-hub-watch
WEB=kr.personal.agent-hub-web
PORT=8787

ts_ip() {
  local ts
  ts="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
  "$ts" ip -4 2>/dev/null | head -1
}

plist() {  # 라벨, 추가 키(xml), 프로그램 인자들...
  local label="$1" extra="$2"; shift 2
  local args=""
  for a in "$@"; do args="$args    <string>$a</string>
"; done
  cat > "$AGENTS/$label.plist" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>WorkingDirectory</key><string>$HUB</string>
$extra
  <key>StandardOutPath</key><string>$HUB/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$HUB/logs/$label.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.npm-global/bin:$HOME/.local/bin</string>
    <key>HOME</key><string>$HOME</string>
    <key>PYTHONUTF8</key><string>1</string>
    <key>LANG</key><string>ko_KR.UTF-8</string>
  </dict>
</dict>
</plist>
XML
}

load() { launchctl bootout "$DOMAIN" "$AGENTS/$1.plist" 2>/dev/null || true; launchctl bootstrap "$DOMAIN" "$AGENTS/$1.plist"; }

case "${1:-status}" in
  install)
    [ -f "$HOME/.agent-hub/web-auth.json" ] || { echo "먼저 비밀번호를 정하세요: python3 web/server.py passwd"; exit 1; }
    IP="$(ts_ip)"
    [ -n "$IP" ] || { echo "Tailscale 주소를 찾지 못했습니다. Tailscale 이 켜져 있는지 확인하세요"; exit 1; }
    mkdir -p "$HUB/logs" "$AGENTS"
    plist "$WATCH" "  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>" "$PY" "$HUB/scripts/hub.py" watch
    plist "$WEB" "  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>" "$PY" "$HUB/web/server.py" serve --host "$IP" --port "$PORT"
    load "$WATCH"; load "$WEB"
    echo "설치됨. 접속: http://$IP:$PORT  (Tailscale 켠 기기에서)"
    ;;
  restart)
    for l in "$WATCH" "$WEB"; do [ -f "$AGENTS/$l.plist" ] && load "$l"; done
    echo "다시 시작함"
    ;;
  status)
    for l in "$WATCH" "$WEB"; do
      if launchctl print "$DOMAIN/$l" >/dev/null 2>&1; then
        echo "$l: $(launchctl print "$DOMAIN/$l" | grep -m1 'state =' | xargs)"
      else
        echo "$l: 설치 안 됨"
      fi
    done
    echo "접속 주소: http://$(ts_ip):$PORT"
    ;;
  uninstall)
    for l in "$WATCH" "$WEB"; do
      launchctl bootout "$DOMAIN" "$AGENTS/$l.plist" 2>/dev/null || true
      rm -f "$AGENTS/$l.plist"
    done
    echo "제거됨"
    ;;
  *) sed -n '2,10p' "$0" ;;
esac
