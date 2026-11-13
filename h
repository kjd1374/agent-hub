#!/bin/bash
# 폰(SSH)에서 짧게 치기 위한 바로가기.  사용: cd ~/agent-hub && ./h s
#   ./h s            작업 목록(status)
#   ./h r T-002      실행(백그라운드)
#   ./h p T-002      분류·배정 미리 보기(plan)
#   ./h n "제목" ...  작업 만들기(new, 옵션은 hub.py new 와 같음)
#   ./h v T-002      작업 파일 보기
#   ./h l T-002      실행 로그 끝부분
#   ./h u            Codex 사용량
#   ./h c            환경 점검(check)
cd "$(dirname "$0")" || exit 1
git pull -q --rebase --autostash 2>/dev/null
cmd="${1:-s}"; shift || true
case "$cmd" in
  s) python3 scripts/hub.py status ;;
  r) scripts/run-task.sh "$@" ;;
  p) python3 scripts/hub.py plan "$@" ;;
  n) python3 scripts/hub.py new "$@" ;;
  v) cat "tasks/${1:?작업 번호}.md" ;;
  l) tail -n 40 "logs/${1:?작업 번호}/run.log" ;;
  u) python3 scripts/hub.py usage ;;
  c) python3 scripts/hub.py check ;;
  *) sed -n '2,11p' "$0" ;;
esac
