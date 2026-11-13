# agent-hub 공통 규칙 (Claude Code·Codex 공용)

이 저장소는 여러 컴퓨터·에이전트가 함께 쓰는 **공용 장부**다. 작업 상태는 `tasks/T-번호.md` 에만 남기고,
실제 작업은 `scripts/hub.py` 가 만든 전용 작업 폴더(`~/agent-work/T-번호`, git worktree)에서 한다.

## 절대 규칙
1. **보호 경로는 읽지도 쓰지도 않는다.** 목록은 `config.json` 의 `protected_paths` 가 기준이다.
   현재: `~/callminer-data`(통화 녹음·분석), `~/Projects/commute-library`(출근길 보관함), `~/NAS`(크롤러),
   `~/korean-friend`, `~/Library/LaunchAgents`, `~/Library/Application Support/LocalVietnamese`, `~/.ssh`, `~/.config/gh`, `~/.codex/auth.json`
2. **시스템·서비스를 건드리지 않는다**: `sudo`, `crontab`, `launchctl`, `pm2`, 전역 패키지 설치, 시스템 설정 변경 금지.
3. **유료 API를 쓰지 않는다.** API 키를 만들거나 등록하거나 코드에 넣지 않는다. 구독 로그인만 쓴다.
4. **비밀 값을 남기지 않는다.** 비밀번호·토큰을 파일·로그·답변·작업 파일에 쓰지 않는다.
5. **지정된 작업 폴더 밖을 고치지 않는다.** commit·push·브랜치 변경은 실행기가 한다.
6. 파일 대량 삭제, `git push --force`, 히스토리 재작성 금지.

## 답변 형식
- 한국어, 짧게. 사용자는 개발자가 아니다.
- 작업이 끝나면 "변경 요약 / 남은 일 / 확인 필요" 를 적는다.

## 이 저장소 자체를 고칠 때 (사람이 Claude Code·Codex로 직접 작업할 때)
- 구조: `scripts/hub.py`(실행기), `scripts/route.py`(배정 규칙), `scripts/agents.py`(CLI 래퍼),
  `scripts/taskfile.py`(작업 파일), `prompts/`(에이전트 지시문), `config.json`, `capabilities.json`
- 파이썬 표준 라이브러리만 쓴다(맥 기본 python3 로 돌아야 함). 3.9 문법까지.
- 고친 뒤 `python3 -m unittest discover -s tests` 통과 확인.
- `config.json` 의 보호 경로를 바꾸면 `python3 scripts/hub.py protect` 로 `.claude/settings.json` 을 다시 만든다.
