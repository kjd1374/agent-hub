#!/usr/bin/env python3
"""agent-hub 실행기 (파이썬 표준 라이브러리만 사용).

  python3 scripts/hub.py check                 환경 점검: 유료 API 키·로그인·디스크·보호 설정
  python3 scripts/hub.py caps                  설치된 스킬·플러그인·MCP 수집 → capabilities.auto.json
  python3 scripts/hub.py protect               config.json 보호 경로 → .claude/settings.json 다시 만들기
  python3 scripts/hub.py new "제목" [옵션]     작업 파일 만들기 (tasks/T-번호.md)
  python3 scripts/hub.py plan T-001            분류·배정만 하고 기록 (실행 안 함)
  python3 scripts/hub.py run T-001             실행 (보통 scripts/run-task.sh 로 백그라운드 실행)
  python3 scripts/hub.py status                작업 목록
  python3 scripts/hub.py release T-001 "메모"  넘김 상태로 (다른 기기·에이전트가 이어받게)
  python3 scripts/hub.py usage                 Codex 구독 사용량 (Claude는 claude 안에서 /usage)
  python3 scripts/hub.py watch                 자동 확인 1회: 실행요청·재시도할 작업을 하나 실행 (launchd 가 5분마다)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB / "scripts"))
import agents  # noqa: E402
import route as router  # noqa: E402
from taskfile import Task  # noqa: E402

TASKS = HUB / "tasks"
PROMPTS = HUB / "prompts"
LOGS = HUB / "logs"
SETTINGS = HUB / ".claude" / "settings.json"
RUNNABLE = ("대기", "실행요청", "넘김", "대기(사용량)")
SHELL_META = re.compile(r"[;&|<>`$\\\n]")
BASH_DENY = ("sudo", "crontab", "launchctl", "pm2", "rm -rf", "git push --force", "git push -f",
             "brew install", "brew uninstall", "npm install -g", "pip install --user")


class Stop(Exception):
    """실행을 멈추고 작업 파일에 상태를 남긴 뒤 끝낸다."""

    def __init__(self, code: int = 1):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------- 공통
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_cfg() -> dict:
    return json.loads((HUB / "config.json").read_text(encoding="utf-8"))


def load_caps() -> dict:
    caps = json.loads((HUB / "capabilities.json").read_text(encoding="utf-8"))
    auto = HUB / "capabilities.auto.json"
    caps["auto"] = json.loads(auto.read_text(encoding="utf-8")) if auto.exists() else {}
    return caps


def expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def sh(cmd, cwd=None, timeout=300, check=False):
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                       timeout=timeout, env=agents.clean_env())
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))} 실패: {(p.stderr or p.stdout).strip()[-500:]}")
    return p


def git(*args, cwd=HUB, check=True, timeout=300):
    return sh(["git", *args], cwd=cwd, check=check, timeout=timeout)


def has_remote(repo: Path) -> bool:
    return bool(git("remote", cwd=repo, check=False).stdout.strip())


def is_protected(path, cfg: dict) -> bool:
    p = expand(path).resolve()
    if p == Path.home().resolve():
        return True
    for q in cfg["protected_paths"]:
        qq = expand(q).resolve()
        if p == qq or qq in p.parents or p in qq.parents:
            return True
    return False


def render(name: str, **vals) -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for k, v in vals.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def extract_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_verdict(text: str) -> tuple[str | None, str]:
    found = re.findall(r"VERDICT:\s*(PASS|CHANGES)", text)
    if not found:
        return None, text.strip()[-2000:]
    verdict = found[-1]
    tail = text[text.rfind("VERDICT:"):].split("\n", 1)
    notes = tail[1].strip() if len(tail) > 1 else ""
    return verdict, notes


def notify(cfg: dict, msg: str) -> None:
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"), method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # 알림 실패는 작업을 멈추지 않는다
        log(f"알림 실패(무시): {e}")


# ---------------------------------------------------------------------------- 공용 장부(git)
def hub_pull() -> None:
    if has_remote(HUB):
        git("pull", "--rebase", "--autostash", check=False, timeout=120)


def hub_push() -> bool:
    if not has_remote(HUB):
        return True
    if git("push", check=False, timeout=120).returncode == 0:
        return True
    git("pull", "--rebase", "--autostash", check=False, timeout=120)
    return git("push", check=False, timeout=120).returncode == 0


def hub_commit(msg: str, paths: list[Path]) -> bool:
    git("add", *[str(p) for p in paths])
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return True
    git("commit", "-m", msg)
    ok = hub_push()
    if not ok:
        log("경고: 공용 장부 push 실패(네트워크?) — 기록은 이 맥에만 있음")
    return ok


def sync(task: Task, msg: str) -> bool:
    task.save()
    return hub_commit(f"{task.id}: {msg}", [task.path])


def load_task(tid: str) -> Task:
    tid = tid.upper() if tid.lower().startswith("t-") else f"T-{int(tid):03d}"
    path = TASKS / f"{tid}.md"
    if not path.exists():
        raise SystemExit(f"작업 파일이 없습니다: {path}")
    return Task(path)


# ---------------------------------------------------------------------------- 점검·설정
def build_settings(cfg: dict) -> dict:
    deny = []
    for p in cfg["protected_paths"]:
        q = p.rstrip("/")
        deny += [f"Read({q})", f"Read({q}/**)", f"Edit({q})", f"Edit({q}/**)"]
    deny += [f"Bash({c}:*)" for c in BASH_DENY]
    return {"permissions": {"deny": deny}}


def preflight(cfg: dict) -> list[tuple[bool, str]]:
    rows: list[tuple[bool, str]] = []
    keys = [k for k in agents.PAID_KEYS if os.environ.get(k)]
    rows.append((not keys, "유료 API 키 변수 없음" if not keys else
                 f"유료 API 키 변수 있음({', '.join(keys)}) → 지우기 전에는 실행 안 함"))
    for name in ("claude", "codex", "git"):
        exe = agents.find(name)
        rows.append((bool(exe), f"{name}: {exe or '찾을 수 없음'}"))
    codex = agents.find("codex")
    if codex:
        st = sh([codex, "login", "status"], timeout=30)
        first = ((st.stdout + st.stderr).strip().splitlines() or ["(응답 없음)"])[0]
        rows.append(("chatgpt" in first.lower(), f"Codex 로그인: {first}"))
    email = git("config", "user.email", check=False).stdout.strip()
    rows.append((bool(email), "git 사용자 설정 있음" if email else "git 사용자 설정 없음 → git config --global user.email 필요"))
    free = shutil.disk_usage(str(Path.home())).free / 1e9
    rows.append((free >= cfg["min_free_gb"], f"디스크 여유 {free:.0f}GB (기준 {cfg['min_free_gb']}GB)"))
    wr = expand(cfg["work_root"])
    rows.append((not is_protected(wr, cfg), f"작업 폴더 {wr} (보호 경로와 겹치지 않아야 함)"))
    current = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else None
    rows.append((current == build_settings(cfg),
                 ".claude/settings.json 보호 규칙 최신" if current == build_settings(cfg)
                 else ".claude/settings.json 이 config.json 과 다름 → python3 scripts/hub.py protect"))
    return rows


def cmd_check(a) -> int:
    rows = preflight(load_cfg())
    for ok, msg in rows:
        print(("✔ " if ok else "✘ ") + msg)
    print("· Claude 로그인은 claude 안에서 /status 로 확인 (Login method 가 구독 계정이어야 함)")
    print("· 사용량: claude 안에서 /usage, Codex 는 python3 scripts/hub.py usage")
    bad = sum(1 for ok, _ in rows if not ok)
    print("점검 통과" if not bad else f"문제 {bad}개")
    return 1 if bad else 0


def cmd_protect(a) -> int:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(build_settings(load_cfg()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"만듦: {SETTINGS}")
    return 0


def _skill_names(root: Path) -> list[str]:
    if not root.exists():
        return []
    names = set()
    for f in root.rglob("SKILL.md"):
        if len(f.relative_to(root).parts) <= 5:
            names.add(f.parent.name)
    names |= {p.name for p in root.iterdir() if p.is_dir()}
    return sorted(n for n in names if not n.startswith("."))


def cmd_caps(a) -> int:
    out: dict = {"collected_at": now(), "host": socket.gethostname(), "claude": {}, "codex": {}}
    claude = agents.find("claude")
    if claude:
        c = out["claude"]
        c["version"] = sh([claude, "--version"], timeout=30).stdout.strip()
        c["skills"] = _skill_names(Path.home() / ".claude" / "skills")
        pl = sh([claude, "plugin", "list"], timeout=60)
        c["plugins_raw"] = (pl.stdout.strip().splitlines()[:60] if pl.returncode == 0
                            else ["(claude plugin list 사용 불가 — claude 안에서 /plugin 으로 확인)"])
        mcp = sh([claude, "mcp", "list"], timeout=120)
        c["mcp"] = [ln.split(":", 1)[0].strip() for ln in mcp.stdout.splitlines()
                    if ":" in ln and not ln.lower().startswith(("checking", "no mcp"))]
        c["names"] = sorted(set(c["skills"]) | set(c["mcp"]))
    codex = agents.find("codex")
    if codex:
        x = out["codex"]
        x["version"] = sh([codex, "--version"], timeout=30).stdout.strip()
        x["skills"] = _skill_names(Path.home() / ".codex" / "skills")
        mcp = sh([codex, "mcp", "list"], timeout=60)
        x["mcp_enabled"] = sorted({ln.split()[0] for ln in mcp.stdout.splitlines()
                                   if ln.strip() and " enabled " in f" {ln} " and not ln.startswith("Name")})
        conf = Path.home() / ".codex" / "config.toml"
        if conf.exists():
            t = conf.read_text(encoding="utf-8", errors="ignore")
            for key in ("model", "model_reasoning_effort"):
                m = re.search(rf'^{key}\s*=\s*"([^"]*)"', t, flags=re.M)
                if m:
                    x[key] = m.group(1)
        x["names"] = sorted(set(x["skills"]) | set(x["mcp_enabled"]))
    path = HUB / "capabilities.auto.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    hub_commit(f"능력 목록 수집 ({out['host']})", [path])
    return 0


def cmd_usage(a) -> int:
    text, _ = agents.codex_usage(0)
    print(f"Codex: {text or '기록 없음(한 번 실행한 뒤 다시 확인)'}")
    print("Claude: claude 안에서 /usage")
    return 0


# ---------------------------------------------------------------------------- 작업 만들기·목록
def next_id() -> str:
    nums = [int(m.group(1)) for f in TASKS.glob("T-*.md") if (m := re.match(r"T-(\d+)\.md$", f.name))]
    return f"T-{(max(nums) + 1 if nums else 1):03d}"


def cmd_new(a) -> int:
    hub_pull()
    tid = next_id()
    text = (TASKS / "TEMPLATE.md").read_text(encoding="utf-8")
    path = TASKS / f"{tid}.md"
    path.write_text(text, encoding="utf-8")
    t = Task(path)
    t.set("id", tid)
    t.set("title", a.title)
    t.set("repo", a.repo)
    t.set("type", a.type)
    t.set("difficulty", a.difficulty)
    t.set("needs", a.needs)
    t.set("assignee", a.assignee)
    t.set("verify", a.verify)
    t.set("created", now())
    if a.goal:
        t.replace_section("목표", a.goal)
    sync(t, "작업 만듦")
    print(f"만듦: {path}\n목표·완료 기준을 채운 뒤: python3 scripts/hub.py plan {tid}")
    return 0


def cmd_status(a) -> int:
    rows = []
    for f in sorted(TASKS.glob("T-*.md")):
        try:
            t = Task(f)
        except ValueError:
            continue
        who = t.get("impl") or t.get("assignee", "auto")
        rows.append(f"{t.id}  {t.get('status', '대기'):<10} {t.get('type', 'auto'):<8} {who:<7} {t.get('title')}")
    print("\n".join(rows) if rows else "작업 없음")
    return 0


def cmd_release(a) -> int:
    hub_pull()
    t = load_task(a.id)
    t.set("status", "넘김")
    t.set("runner", "")
    t.append("남은 일", f"- {now()} 넘김: {a.memo or '(메모 없음)'}")
    sync(t, "넘김")
    print(f"{t.id} 넘김 상태로 바꿈")
    return 0


# ---------------------------------------------------------------------------- 분류·배정
def ensure_classified(t: Task, cfg: dict, caps: dict) -> None:
    if t.get("type", "auto") != "auto" and t.get("difficulty", "auto") != "auto":
        return
    log("분류: Codex(낮은 추론 강도)")
    prompt = render("classify", title=t.get("title"), repo=t.get("repo"),
                    goal=t.section("목표") or "(없음)", criteria=t.section("완료 기준") or "(없음)",
                    known_needs=", ".join(caps.get("known_needs", [])))
    d = LOGS / t.id
    n = len(list(d.glob("*.log"))) + 1 if d.exists() else 1
    r = agents.run_codex(prompt, HUB, d / f"{n:02d}-{datetime.now():%m%d-%H%M}-classify-codex.log",
                         write=False, effort=cfg["effort"]["classify"], timeout=600)
    data = extract_json(r.text) if r.ok else None
    if data:
        if t.get("type", "auto") == "auto":
            t.set("type", data.get("type") if data.get("type") in router.TYPES else "general")
        if t.get("difficulty", "auto") == "auto":
            t.set("difficulty", data.get("difficulty") if data.get("difficulty") in ("하", "중", "상") else "중")
        if not t.get("needs") and isinstance(data.get("needs"), list):
            t.set("needs", ", ".join(str(n) for n in data["needs"]))
        lines = [f"- {now()} Codex 분류: {data.get('summary', '')} → {t.get('type')} / 난도 {t.get('difficulty')}"]
        for s in data.get("subtasks") or []:
            if isinstance(s, dict):
                lines.append(f"  - 하위 작업 제안: {s.get('title')} ({s.get('type')}, 필요 능력: {s.get('needs') or '-'})")
        t.append("분류", "\n".join(lines))
    else:
        why = "사용량 한도" if r.limit else (r.error or "JSON 아님")
        if t.get("type", "auto") == "auto":
            t.set("type", "general")
        if t.get("difficulty", "auto") == "auto":
            t.set("difficulty", "중")
        t.append("분류", f"- {now()} 자동 분류 실패({why}) → 기본값 general / 중. 필요하면 직접 고칠 것")


def decide(t: Task, cfg: dict, caps: dict) -> dict:
    _, used = agents.codex_usage(0)
    plan = router.route(t.meta, caps, used, cfg["codex_busy_percent"])
    head = {"run": f"{plan['impl']} 작성 → {plan['reviewer']} 검증" + (" (Claude 설계 먼저)" if plan["design"] else ""),
            "dot": "돌쇠가 바로 답함", "blocked": "실행 불가"}[plan["mode"]]
    t.append("배정 기록", f"- {now()} {head}\n" + "\n".join(f"  - {r}" for r in plan["reasons"]))
    return plan


def cmd_plan(a) -> int:
    cfg, caps = load_cfg(), load_caps()
    hub_pull()
    t = load_task(a.id)
    ensure_classified(t, cfg, caps)
    plan = decide(t, cfg, caps)
    sync(t, "분류·배정")
    print(t.section("분류"))
    print(t.section("배정 기록"))
    return 0 if plan["mode"] != "blocked" else 1


# ---------------------------------------------------------------------------- 실행
class Runner:
    def __init__(self, t: Task, cfg: dict, caps: dict):
        self.t, self.cfg, self.caps = t, cfg, caps
        self.repo = expand(t.get("repo") or "~/agent-hub")
        self.wt: Path | None = None

    # -- 기록·멈춤
    def stop(self, status: str, remain: str, code: int = 1):
        if status == "대기(사용량)":  # 자동 확인(watch)이 일정 시간 뒤 다시 시도. 너무 많이 실패하면 사람에게 넘김
            tries = int(self.t.get("auto_retries") or 0) + 1
            self.t.set("auto_retries", tries)
            if tries > self.cfg.get("max_auto_retries", 12):
                status, remain = "보고필요", remain + f"\n- 자동 재시도 {tries - 1}회 모두 한도 → 사람이 확인"
            else:
                due = datetime.now() + timedelta(minutes=self.cfg.get("retry_minutes", 60))
                self.t.set("retry_after", due.strftime("%Y-%m-%d %H:%M"))
                remain += f"\n- 자동 재시도 예정: {due:%m-%d %H:%M} 이후"
        self.t.set("status", status)
        self.t.replace_section("남은 일", remain)
        sync(self.t, status)
        notify(self.cfg, f"[agent-hub] {self.t.id} {status}: {self.t.get('title')}")
        log(f"멈춤: {status}")
        raise Stop(code)

    # -- 작업 폴더
    def prepare_worktree(self) -> None:
        t = self.t
        if is_protected(self.repo, self.cfg):
            self.stop("보고필요", f"- 대상 폴더 {self.repo} 가 보호 경로와 겹침 → 실행 안 함")
        if not (self.repo / ".git").exists():
            self.stop("보고필요", f"- 대상 폴더 {self.repo} 가 git 저장소가 아님")
        branch = t.get("branch") or f"task/{t.id}"
        wt = expand(self.cfg["work_root"]) / t.id
        if not wt.exists():
            wt.parent.mkdir(parents=True, exist_ok=True)

            def exists(ref):
                return git("rev-parse", "--verify", "--quiet", ref, cwd=self.repo, check=False).returncode == 0

            if not exists(branch) and has_remote(self.repo):  # 다른 기기에서 하던 작업이면 원격 브랜치에서 이어받기
                git("fetch", "origin", branch, cwd=self.repo, check=False, timeout=120)
                if exists(f"origin/{branch}"):
                    git("branch", branch, f"origin/{branch}", cwd=self.repo)
            if exists(branch):
                git("worktree", "add", str(wt), branch, cwd=self.repo)
            else:
                git("worktree", "add", "-b", branch, str(wt), "HEAD", cwd=self.repo)
        self.wt = wt
        t.set("branch", branch)
        t.set("worktree", str(wt))
        if not t.get("base"):
            t.set("base", git("rev-parse", "HEAD", cwd=wt).stdout.strip())

    def commit_wt(self, msg: str) -> bool:
        git("add", "-A", cwd=self.wt)
        if git("diff", "--cached", "--quiet", cwd=self.wt, check=False).returncode == 0:
            return False
        git("commit", "-m", msg, cwd=self.wt)
        return True

    def diff(self) -> str:
        rng = f"{self.t.get('base')}..HEAD"
        d = git("diff", rng, cwd=self.wt).stdout
        if len(d) > self.cfg["max_diff_chars"]:
            stat = git("diff", "--stat", rng, cwd=self.wt).stdout
            d = stat + "\n(변경이 길어 앞부분만 넣음. 필요한 파일은 직접 읽을 것)\n" + d[: self.cfg["max_diff_chars"]]
        return d

    def verify(self) -> tuple[bool | None, str]:
        cmd = self.t.get("verify")
        if not cmd:
            return None, "(검증 명령 없음)"
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(self.wt), capture_output=True, text=True,
                               timeout=self.cfg["verify_timeout_sec"], env=agents.clean_env())
            ok, out = p.returncode == 0, (p.stdout + p.stderr)[-3000:]
            res = "통과" if ok else f"실패(코드 {p.returncode})"
        except subprocess.TimeoutExpired:
            ok, out, res = False, "시간 초과", "시간 초과"
        self.t.append("검증", f"- {now()} 검증 명령 `{cmd}`: {res}")
        return ok, out

    # -- 에이전트 실행
    def call(self, step: str, agent: str, prompt: str, write: bool, effort: str) -> agents.Result:
        log(f"{step} → {agent}")
        d = LOGS / self.t.id
        n = len(list(d.glob("*.log"))) + 1 if d.exists() else 1
        logf = d / f"{n:02d}-{datetime.now():%m%d-%H%M}-{step}-{agent}.log"
        timeout = self.cfg["agent_timeout_sec"]
        if agent == "claude":
            r = agents.run_claude(prompt, self.wt, logf, mode="write" if write else "read",
                                  settings=SETTINGS, timeout=timeout)
        else:
            r = agents.run_codex(prompt, self.wt, logf, write=write, effort=effort, timeout=timeout)
        usage = r.usage or ("확인 불가(claude 안에서 /usage)" if agent == "claude" else "확인 불가")
        self.t.append("사용량 기록", f"- {now()} {agent} {step}: {usage} · {r.seconds:.0f}초")
        state = "성공" if r.ok else ("사용량 한도" if r.limit else f"실패: {r.error}")
        self.t.append("한 일", f"- {now()} [{agent}] {step}: {state} ({r.seconds:.0f}초) · 로그 {logf.relative_to(HUB)}")
        return r

    def call_with_fallback(self, step: str, agent: str, prompt: str, write: bool, effort: str) -> tuple[agents.Result, str]:
        """한도에 걸리면 고정되지 않은 작성 단계만 다른 에이전트로 넘긴다. 검증은 넘기지 않는다(자기 검증 방지)."""
        r = self.call(step, agent, prompt, write, effort)
        if r.ok:
            return r, agent
        if r.limit:
            alt = router.other(agent)
            pinned = self.t.get("pinned")
            if step != "review" and pinned != agent:
                self.t.append("배정 기록", f"- {now()} {agent} 사용량 한도 → {step}을(를) {alt}에게 넘김")
                r2 = self.call(step, alt, prompt, write, effort)
                if r2.ok:
                    return r2, alt
            self.stop("대기(사용량)", f"- {step} 단계에서 사용량 한도. 리셋 후 다시 실행: `scripts/run-task.sh {self.t.id}`", 3)
        self.stop("보고필요", f"- {step} 단계 실패: {r.error or '원인 불명'}\n- 로그: {r.log}")
        raise AssertionError  # stop() 이 항상 예외를 던진다

    # -- 전체 흐름
    def run(self, plan: dict) -> None:
        t, cfg = self.t, self.cfg
        self.prepare_worktree()
        t.set("pinned", plan["pinned"] or "")
        if not t.get("impl"):
            t.set("impl", plan["impl"])
        sync(t, f"시작 ({socket.gethostname()})")
        ctx = dict(id=t.id, title=t.get("title"), type=t.get("type"), branch=t.get("branch"),
                   goal=t.section("목표") or "(없음)", criteria=t.section("완료 기준") or "(없음)",
                   verify=t.get("verify") or "(없음)")
        stage = t.get("stage")
        rnd = int(t.get("round") or 0)

        if stage == "" and plan["design"]:
            r, _ = self.call_with_fallback("design", "claude", render("design", **ctx), False, cfg["effort"]["implement"]["상"])
            t.replace_section("설계 메모", r.text.strip()[:6000])
            t.set("stage", stage := "designed")
            sync(t, "설계 메모")

        if stage in ("", "designed"):
            design = t.section("설계 메모")
            prompt = render("implement" if t.get("type") == "code" else "do", **ctx,
                            design=f"설계 메모(이대로 따를 것):\n{design}" if design else "")
            impl = t.get("impl")
            eff = cfg["effort"]["implement"].get(t.get("difficulty"), "medium")
            r, impl = self.call_with_fallback("implement", impl, prompt, True, eff)
            t.set("impl", impl)
            t.replace_section("최근 결과", f"[{impl} 작성]\n{r.text.strip()[:3000]}")
            changed = self.commit_wt(f"{t.id} [작업] {impl} 작성")
            if not changed:
                if t.get("type") == "code":
                    self.stop("보고필요", "- 구현 단계에서 바뀐 파일이 없음. '최근 결과' 섹션의 답변 확인 필요")
                t.set("stage", "done")
                self.stop("완료", "- 파일 변경 없이 답변만 나옴(검토 없음). 결과는 '최근 결과' 섹션", 0)
            t.set("stage", stage := "implemented")
            sync(t, f"{impl} 작성")

        while stage == "implemented":
            ok, vout = self.verify()
            reviewer = router.other(t.get("impl"))
            prompt = render("review", **ctx, verify_result=vout, diff=self.diff())
            r, _ = self.call_with_fallback("review", reviewer, prompt, False, cfg["effort"]["review"])
            verdict, notes = parse_verdict(r.text)
            if verdict == "PASS" and ok is False:
                verdict, notes = "CHANGES", f"리뷰는 통과했지만 검증 명령이 실패함:\n{vout[-1500:]}"
            t.replace_section("최근 리뷰", f"{now()} {reviewer} 판정: {verdict or '판정 없음'}\n\n{notes[:4000]}")
            t.append("검증", f"- {now()} {reviewer} 리뷰 {rnd + 1}회차: {verdict or '판정 없음'}")
            if verdict == "PASS":
                t.set("stage", stage := "passed")
                break
            if verdict is None:
                self.stop("보고필요", "- 리뷰 답변에 VERDICT 줄이 없음. '최근 리뷰' 확인 필요")
            rnd += 1
            t.set("round", rnd)
            if rnd > cfg["review_rounds"]:
                self.stop("보고필요", f"- 검증·수정 {cfg['review_rounds']}회 후에도 통과 못 함. '최근 리뷰'를 보고 결정할 것\n"
                                    f"- 이어서 고치게 하려면 round 를 0으로 바꾸고 다시 실행")
            sync(t, f"리뷰 {rnd}회차: 수정 필요")
            impl = t.get("impl")
            r, impl = self.call_with_fallback("fix", impl, render("fix", **ctx, review_notes=notes), True, cfg["effort"]["fix"])
            t.set("impl", impl)
            t.replace_section("최근 결과", f"[{impl} 수정 {rnd}회차]\n{r.text.strip()[:3000]}")
            self.commit_wt(f"{t.id} [작업] {impl} 수정 {rnd}회차")
            sync(t, f"{impl} 수정 {rnd}회차")

        pushed = ""
        if cfg.get("push_branches") and has_remote(self.repo):
            p = git("push", "-u", "origin", t.get("branch"), cwd=self.wt, check=False, timeout=180)
            pushed = "원격에 브랜치 올림" if p.returncode == 0 else "원격 push 실패(로컬에만 있음)"
        self.stop("검증완료",
                  f"- 사용자 확인 후 병합: `git -C {self.repo} merge {t.get('branch')}`\n"
                  f"- 결과 보기: `cd {self.wt}`\n"
                  f"- 정리: `git -C {self.repo} worktree remove {self.wt}`\n"
                  + (f"- {pushed}\n" if pushed else ""), 0)


def task_problems(t: Task, cfg: dict) -> list[str]:
    """돌쇠 등 밖에서 쓴 작업 파일도 안전하게: 허용된 저장소만, 검증 명령은 허용 목록만."""
    probs = []
    repo = expand(t.get("repo") or "~/agent-hub").resolve()
    allowed = [expand(r).resolve() for r in cfg.get("allowed_repos", [])]
    if repo not in allowed:
        probs.append(f"대상 저장소 {t.get('repo')} 가 config.json 의 allowed_repos 에 없음")
    cmd = t.get("verify")
    if cmd:
        prefixes = cfg.get("verify_allowed", [])
        if SHELL_META.search(cmd) or not any(cmd == p or cmd.startswith(p + " ") for p in prefixes):
            probs.append(f"검증 명령 `{cmd}` 가 허용 목록(config.json 의 verify_allowed)에 없거나 특수문자(; | & > $ 등)를 씀")
    return probs


def cmd_run(a) -> int:
    return run_task(a.id, force=a.force, restart=a.restart)


def run_task(tid: str, force: bool = False, restart: bool = False) -> int:
    cfg, caps = load_cfg(), load_caps()
    LOGS.mkdir(exist_ok=True)
    lockf = open(HUB / ".run.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("다른 작업이 실행 중 → 끝난 뒤 다시 실행 (에이전트는 한 번에 하나만)")
        return 3
    bad = [msg for ok, msg in preflight(cfg) if not ok]
    if bad:
        for msg in bad:
            log(f"점검 실패: {msg}")
        return 2
    hub_pull()
    t = load_task(tid)
    host = socket.gethostname()
    st = t.get("status", "대기")
    if st not in RUNNABLE and not (st == "진행" and t.get("runner") == host) and not force:
        log(f"{t.id} 상태가 '{st}' → 실행 안 함 (강제로 하려면 --force)")
        return 1
    probs = task_problems(t, cfg)
    if probs:
        t.set("status", "보고필요")
        t.replace_section("남은 일", "\n".join(f"- 실행 거부: {p}" for p in probs))
        sync(t, "실행 거부")
        notify(cfg, f"[agent-hub] {t.id} 실행 거부: {t.get('title')}")
        for p in probs:
            log(f"실행 거부: {p}")
        return 1
    if restart:
        for k in ("stage", "round", "impl"):
            t.set(k, "")
    try:
        ensure_classified(t, cfg, caps)
        plan = decide(t, cfg, caps)
        if plan["mode"] == "dot":
            t.set("status", "돌쇠로")
            t.replace_section("남은 일", "- 단순 질문: 돌쇠에게 그대로 물어볼 것")
            sync(t, "돌쇠로")
            return 0
        if plan["mode"] == "blocked":
            t.set("status", "보고필요")
            t.replace_section("남은 일", "- 배정 불가: '배정 기록' 확인")
            sync(t, "배정 불가")
            return 1
        t.set("status", "진행")
        t.set("runner", host)
        t.set("started", now())
        if not sync(t, f"가져감 ({host})"):
            hub_pull()
            fresh = load_task(a.id)
            if fresh.get("runner") not in ("", host):
                log(f"다른 기기({fresh.get('runner')})가 먼저 가져감 → 중단")
                return 1
        Runner(t, cfg, caps).run(plan)
    except Stop as s:
        return s.code
    except Exception as e:  # 예상 못 한 오류도 작업 파일에 남긴다
        t.set("status", "보고필요")
        t.replace_section("남은 일", f"- 실행기 오류: {e}")
        sync(t, "실행기 오류")
        notify(cfg, f"[agent-hub] {t.id} 실행기 오류: {e}")
        raise
    return 0


def watch_candidates(cutoff: str) -> list[str]:
    """실행요청 작업, 그리고 재시도 시각이 지난 대기(사용량) 작업. 번호 순."""
    ids = []
    for f in sorted(TASKS.glob("T-*.md")):
        try:
            t = Task(f)
        except ValueError:
            continue
        st = t.get("status")
        if st == "실행요청" or (st == "대기(사용량)" and t.get("retry_after") and t.get("retry_after") <= cutoff):
            ids.append(t.id)
    return ids


def cmd_watch(a) -> int:
    """launchd 가 5분마다 부른다. 할 일이 없으면 아무것도 출력하지 않고 끝난다(로그가 쌓이지 않게)."""
    hub_pull()
    ids = watch_candidates(now())
    if not ids:
        return 0
    log(f"자동 확인: {', '.join(ids)} 중 {ids[0]} 실행")
    return run_task(ids[0])


# ---------------------------------------------------------------------------- 진입점
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("caps").set_defaults(fn=cmd_caps)
    sub.add_parser("protect").set_defaults(fn=cmd_protect)
    sub.add_parser("usage").set_defaults(fn=cmd_usage)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    p = sub.add_parser("new")
    p.add_argument("title")
    p.add_argument("--repo", default="~/agent-hub", help="작업할 git 저장소(맥미니 경로)")
    p.add_argument("--type", default="auto", choices=("auto",) + router.TYPES)
    p.add_argument("--difficulty", default="auto", choices=("auto", "하", "중", "상"))
    p.add_argument("--needs", default="", help="필요 능력(쉼표로)")
    p.add_argument("--assignee", default="auto", choices=("auto", "claude", "codex"))
    p.add_argument("--verify", default="", help="검증 명령(예: npm test)")
    p.add_argument("--goal", default="", help="목표(나중에 파일에서 써도 됨)")
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("plan")
    p.add_argument("id")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("run")
    p.add_argument("id")
    p.add_argument("--force", action="store_true", help="상태와 상관없이 실행")
    p.add_argument("--restart", action="store_true", help="처음 단계부터 다시")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("release")
    p.add_argument("id")
    p.add_argument("memo", nargs="?", default="")
    p.set_defaults(fn=cmd_release)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
