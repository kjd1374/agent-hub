# agent-hub

여러 컴퓨터와 AI 에이전트(Claude Code, Codex, 돌쇠)를 하나로 묶는 **공용 장부 + 실행기**.

- 작업 1건 = `tasks/T-번호.md` 파일 1개. 상태·배정 이유·한 일·검증·남은 일·사용량이 전부 여기 남는다.
- 실제 작업은 맥미니의 전용 작업 폴더 `~/agent-work/T-번호` (git worktree)에서. 원래 폴더는 안 건드린다.
- 기본 흐름: **Codex 분류 → 규칙으로 배정 → Codex 작성 → Claude 검증 → (최대 2번) 수정 → 검증완료**.
- 유료 API 경로 없음. API 키 변수가 있으면 실행 자체를 거부한다.
- 보호 경로(`config.json`): 통화 녹음·분석, 출근길 보관함, NAS 크롤러 등은 에이전트가 읽지도 쓰지도 못하게 막는다.

## 처음 한 번 (맥미니)

```bash
cd ~ && gh repo clone kjd1374/agent-hub && cd agent-hub
```

```bash
python3 scripts/hub.py check
```

```bash
python3 scripts/hub.py caps
```

`check` 는 전부 ✔ 여야 한다. `caps` 는 설치된 스킬·플러그인·MCP 를 모아 `capabilities.auto.json` 에 저장하고 push 한다.

## 매일 쓰는 명령 (모두 ~/agent-hub 에서)

| 하고 싶은 것 | 명령 |
|---|---|
| 작업 만들기 | `python3 scripts/hub.py new "제목" --repo ~/작업할저장소 --verify "npm test"` |
| 분류·배정만 미리 보기 | `python3 scripts/hub.py plan T-001` |
| 실행(백그라운드, 바로 끝남) | `scripts/run-task.sh T-001` |
| 진행 상황 | `python3 scripts/hub.py status` |
| 로그 보기 | `tail -f logs/T-001/run.log` |
| 다른 기기로 넘기기 | `python3 scripts/hub.py release T-001 "메모"` |
| Codex 사용량 | `python3 scripts/hub.py usage` (Claude 는 claude 안에서 `/usage`) |

작업 파일을 만든 뒤 `## 목표` 와 `## 완료 기준` 을 채우면 결과가 좋아진다. `type`·`difficulty` 를 비워 두면(auto) Codex 가 낮은 추론 강도로 분류한다.

## 상태별로 할 일

| 상태 | 뜻 | 할 일 |
|---|---|---|
| 대기 | 아직 시작 안 함 | `scripts/run-task.sh T-번호` |
| 진행 | 실행 중 | 기다림. `status`·로그로 확인 |
| 대기(사용량) | 두 에이전트 모두 한도 | 리셋 후 같은 명령으로 다시 실행(멈춘 단계부터 이어감) |
| 검증완료 | Claude(또는 반대쪽) 검증 통과 | 작업 파일 `## 남은 일` 의 병합 명령으로 확인 후 병합 |
| 보고필요 | 실패·판정 불가·2번 수정해도 미통과 | `## 최근 리뷰`·`## 남은 일` 보고 결정 |
| 돌쇠로 | 단순 질문 | 돌쇠에게 그대로 묻기 |
| 넘김 | 다른 기기·에이전트가 이어받을 차례 | 이어받을 곳에서 실행 |

## 배정 규칙 (scripts/route.py)

1. 단순 질문 → 돌쇠가 바로 답함(실행 안 함)
2. 필요 능력(`needs`)이 한 에이전트에만 있으면 그쪽 고정, 아무에게도 없으면 막고 설치 필요라고 알림
3. 작업 파일의 `assignee` 지정
4. 코딩 → Codex 작성 + Claude 검증, 난도 상이면 Claude 가 짧은 설계 메모 먼저
5. 그 외 유형 → `capabilities.json` 의 `type_defaults`, 검증은 반대쪽
6. Codex 사용량이 `codex_busy_percent`(기본 90%) 이상이면 고정 안 된 Codex 몫을 Claude 로
7. 실행 중 한도 → 작성·수정 단계만 반대쪽으로 넘김. 검증은 넘기지 않고 대기(자기 검증 방지)

## 돌쇠·폰에서 부를 때

맥미니 터미널에 접근할 수 있으면(돌쇠 로컬 접근, SSH, Claude Remote Control) 아래 한 줄이면 된다.

```bash
cd ~/agent-hub && git pull && scripts/run-task.sh T-001
```

결과 확인은 `tasks/T-001.md` 를 읽으면 된다(GitHub 에서도 보임).

## 주의

- `verify` 명령은 샌드박스 밖에서 사용자 권한으로 실행된다. 직접 쓴 명령만 넣을 것.
- 에이전트는 한 번에 하나만 실행된다(맥미니 메모리·크롤러 보호).
- 보호 경로를 바꾸면 `python3 scripts/hub.py protect` 로 `.claude/settings.json` 을 다시 만든다.
- Codex 는 쓰기만 작업 폴더로 제한되고 읽기 차단은 규칙 파일(AGENTS.md)에 의존한다. 녹음 데이터를 확실히 막으려면 에이전트 전용 macOS 계정이 필요하다(3단계에서 판단).
- 이 저장소 코드를 고친 뒤: `python3 -m unittest discover -s tests`
