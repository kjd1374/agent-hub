"""Claude Code·Codex CLI 실행 래퍼. 구독 로그인만 쓰고 API 키는 쓰지 않는다."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# 이 변수가 있으면 CLI가 구독 대신 API로 과금될 수 있다 → 실행기는 시작을 거부하고, 자식 프로세스에서도 지운다.
PAID_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")

# 사용량 한도에 걸렸을 때 CLI가 내는 문구(소문자로 비교)
LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "hit your limit", "you've hit your",
               "quota exceeded", "too many requests", "429", "사용량 한도")

_EXTRA_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.local/bin",
                "~/.claude/local", "/Applications/Codex.app/Contents/Resources")


def find(name: str) -> str | None:
    exe = shutil.which(name)
    if exe:
        return exe
    for d in _EXTRA_PATHS:
        p = Path(os.path.expanduser(d)) / name
        if p.exists():
            return str(p)
    return None


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in PAID_KEYS}
    extra = [os.path.expanduser(d) for d in _EXTRA_PATHS]
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + extra)
    return env


@dataclass
class Result:
    agent: str
    ok: bool
    text: str  # 에이전트의 마지막 답변(없으면 출력 끝부분)
    limit: bool  # 사용량 한도로 실패
    seconds: float
    log: Path
    usage: str = ""  # 구독 사용량 표시(모르면 빈 문자열)
    usage_percent: float | None = None
    error: str = field(default="")


_STRONG = ("usage limit", "limit reached", "hit your limit", "you've hit your", "사용량 한도")


def _is_limit(returncode: int, text: str) -> bool:
    """실패했으면 한도 문구가 있는지 본다. 성공(코드 0)인데 답이 아주 짧고 한도 문구뿐이면 그것도 한도로 본다.
    (정상 답변 속 'rate limit' 같은 단어를 한도로 오인하지 않게 성공 시에는 강한 문구만 본다.)"""
    low = text.lower()
    if returncode != 0:
        return any(w in low for w in LIMIT_WORDS)
    return len(low.strip()) < 300 and any(w in low for w in _STRONG)


def run_claude(prompt: str, cwd: Path, log: Path, *, mode: str, settings: Path, timeout: int) -> Result:
    """mode: 'write' = 작업 폴더 안 파일 수정 허용, 'read' = 읽기만(설계·검증)."""
    exe = find("claude")
    if not exe:
        return Result("claude", False, "", False, 0, log, error="claude 명령을 찾을 수 없음")
    cmd = [exe, "-p", "--output-format", "text", "--settings", str(settings)]
    if mode == "write":
        cmd += ["--permission-mode", "acceptEdits"]
    else:
        cmd += ["--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"]
    return _run("claude", cmd, prompt, cwd, log, timeout)


def run_codex(prompt: str, cwd: Path, log: Path, *, write: bool, effort: str, timeout: int) -> Result:
    exe = find("codex")
    if not exe:
        return Result("codex", False, "", False, 0, log, error="codex 명령을 찾을 수 없음")
    cmd = [exe, "exec", "--json", "--skip-git-repo-check", "-C", str(cwd),
           "-s", "workspace-write" if write else "read-only",
           "-c", f'model_reasoning_effort="{effort}"']
    wall0 = time.time()
    r = _run("codex", cmd, prompt, cwd, log, timeout)
    r.text = _codex_final(r.text) or r.text[-4000:]
    r.usage, r.usage_percent = codex_usage(since=wall0 - 1)
    return r


def _run(agent: str, cmd: list, prompt: str, cwd: Path, log: Path, timeout: int) -> Result:
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    try:
        # 프롬프트는 stdin으로(길이 제한·특수문자 문제 없음)
        p = subprocess.run(cmd, input=prompt, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, env=clean_env())
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, code = f"시간 초과({timeout}초)", -1
    dt = time.perf_counter() - t0
    log.write_text(f"$ {' '.join(cmd)}\n--- prompt ---\n{prompt}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n",
                   encoding="utf-8")
    limit = _is_limit(code, out[-3000:] + "\n" + err)
    error = "" if code == 0 else (err.strip().splitlines() or ["(오류 메시지 없음)"])[-1][:300]
    return Result(agent, code == 0 and not limit, out, limit, dt, log, error=error)


def _codex_final(stdout: str) -> str:
    """codex exec --json 이벤트에서 마지막 에이전트 답변을 꺼낸다."""
    last = ""
    for ln in stdout.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        stack = [ev]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("type") in ("agent_message", "assistant_message") and isinstance(x.get("text"), str):
                    last = x["text"]
                elif x.get("type") == "agent_message" and isinstance(x.get("message"), str):
                    last = x["message"]
                stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
            elif isinstance(x, list):
                stack.extend(x)
    return last


def codex_usage(since: float = 0) -> tuple[str, float | None]:
    """Codex 세션 기록(~/.codex/sessions)의 rate_limits 에서 구독 사용량(%)을 읽는다.
    since 이후 기록이 없으면 빈 값. 형식이 바뀌면 빈 값(실행은 계속)."""
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return "", None
    files = sorted((f for f in root.rglob("*.jsonl") if f.stat().st_mtime >= since), key=lambda f: f.stat().st_mtime)
    if not files:
        return "", None
    last = None
    for ln in files[-1].read_text(encoding="utf-8", errors="ignore").splitlines():
        if "rate_limits" not in ln:
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        stack = [ev]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if isinstance(x.get("rate_limits"), dict):
                    last = x["rate_limits"]
                stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
            elif isinstance(x, list):
                stack.extend(x)
    if not last:
        return "", None
    parts, top = [], None
    for key in ("primary", "secondary"):
        r = last.get(key)
        if isinstance(r, dict) and "used_percent" in r:
            win = r.get("window_minutes") or 0
            label = f"{win // 1440}일" if win >= 1440 else f"{win // 60}시간"
            pct = float(r["used_percent"])
            parts.append(f"{label} 기준 {pct:.0f}% 사용")
            top = pct if top is None else max(top, pct)
    return ", ".join(parts), top
