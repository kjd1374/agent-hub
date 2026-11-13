#!/bin/bash
# 작업을 백그라운드로 실행하고 바로 끝난다(돌쇠·SSH·폰에서 한 줄로 부르기 좋게).
#   scripts/run-task.sh T-001            실행
#   scripts/run-task.sh T-001 --restart  처음 단계부터 다시
# 진행 상황: python3 scripts/hub.py status   /   로그: logs/T-001/run.log
set -euo pipefail
HUB="$(cd "$(dirname "$0")/.." && pwd)"
ID="${1:?작업 번호를 주세요. 예: scripts/run-task.sh T-001}"
shift
mkdir -p "$HUB/logs/$ID"
nohup python3 "$HUB/scripts/hub.py" run "$ID" "$@" >> "$HUB/logs/$ID/run.log" 2>&1 &
echo "시작: $ID (pid $!)"
echo "진행: python3 $HUB/scripts/hub.py status"
echo "로그: tail -f $HUB/logs/$ID/run.log"
