#!/usr/bin/env python3
"""agent-hub 웹 창구 (파이썬 표준 라이브러리만).

  python3 web/server.py passwd                         비밀번호 설정(처음 한 번, 맥미니에서)
  python3 web/server.py serve --host 100.69.42.127     Tailscale 주소에서만 열기 (scripts/services.sh 가 실행)

엔진은 scripts/hub.py 를 그대로 쓴다. 이 서버는 화면과 버튼만 담당한다.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
HUB_DIR = HERE.parent
sys.path.insert(0, str(HUB_DIR / "scripts"))
import hub  # noqa: E402
from taskfile import Task  # noqa: E402

AUTH = Path.home() / ".agent-hub" / "web-auth.json"
SESSION_DAYS = 30
SECTIONS = ("목표", "완료 기준", "최근 결과", "최근 리뷰", "남은 일", "배정 기록", "분류", "설계 메모", "한 일", "검증", "사용량 기록")
EDITABLE = ("검증완료", "보고필요", "대기(사용량)", "취소", "대기")

SESSIONS: dict[str, float] = {}
JOBS: dict[str, dict] = {}
_kick_lock = threading.Lock()


# ---------------------------------------------------------------------------- 비밀번호·세션
def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def set_password() -> int:
    pw = getpass.getpass("새 비밀번호(8자 이상): ")
    if len(pw) < 8 or pw != getpass.getpass("한 번 더: "):
        print("비밀번호가 짧거나 서로 다릅니다")
        return 1
    salt = secrets.token_hex(16)
    AUTH.parent.mkdir(mode=0o700, exist_ok=True)
    AUTH.write_text(json.dumps({"salt": salt, "hash": _hash(pw, salt)}), encoding="utf-8")
    os.chmod(AUTH, 0o600)
    print(f"저장함: {AUTH}")
    return 0


def check_password(pw: str) -> bool:
    if not AUTH.exists():
        return False
    data = json.loads(AUTH.read_text(encoding="utf-8"))
    return hmac.compare_digest(_hash(pw, data["salt"]), data["hash"])


# ---------------------------------------------------------------------------- 백그라운드 일
def start_job(title: str, fn) -> str:
    jid = secrets.token_hex(6)
    job = {"id": jid, "title": title, "lines": [], "done": False, "ok": None, "result": None}
    JOBS[jid] = job

    def say(msg: str) -> None:
        job["lines"].append(f"{datetime.now():%H:%M:%S} {msg}")

    def run() -> None:
        try:
            job["result"] = fn(say)
            job["ok"] = True
        except Exception as e:  # 화면에 그대로 보여준다
            job["ok"] = False
            say(f"실패: {e}")
        finally:
            job["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jid


def kick() -> None:
    """실행 대기 작업을 바로 처리하게 hub.py watch 를 띄운다(이미 실행 중이면 그쪽이 이어서 처리)."""
    with _kick_lock:
        hub.LOGS.mkdir(exist_ok=True)
        out = open(hub.LOGS / "watch.log", "a")
        subprocess.Popen([sys.executable, str(HUB_DIR / "scripts" / "hub.py"), "watch"], cwd=str(HUB_DIR),
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


# ---------------------------------------------------------------------------- 데이터
def all_tasks() -> list[Task]:
    out = []
    for f in sorted(hub.TASKS.glob("T-*.md")):
        try:
            out.append(Task(f))
        except ValueError:
            continue
    return out


def task_project(t: Task, repo_map: dict) -> str:
    if t.get("project"):
        return t.get("project")
    return repo_map.get(str(hub.expand(t.get("repo") or "~/agent-hub").resolve()), "")


def repo_map() -> dict:
    return {str(hub.expand(p["repo"]).resolve()): n for n, p in hub.load_projects().items()}


def task_row(t: Task) -> dict:
    return {"id": t.id, "title": t.get("title"), "status": t.get("status", "대기"), "type": t.get("type"),
            "impl": t.get("impl"), "created": t.get("created"), "after": t.get("after"), "origin": t.get("origin")}


def overview() -> dict:
    rm = repo_map()
    projects = hub.load_projects()
    counts: dict[str, dict] = {n: {} for n in projects}
    running = None
    for t in all_tasks():
        p = task_project(t, rm)
        st = t.get("status", "대기")
        if p in counts:
            counts[p][st] = counts[p].get(st, 0) + 1
        if st == "진행":
            running = {"id": t.id, "title": t.get("title"), "project": p}
    usage, _ = hub.agents.codex_usage(0)
    return {"projects": [{"name": n, **p, "counts": counts[n]} for n, p in projects.items()],
            "running": running, "usage": {"codex": usage or "기록 없음", "claude": "claude 안에서 /usage"}}


def project_tasks(name: str) -> list[dict]:
    rm = repo_map()
    rows = [task_row(t) for t in all_tasks() if task_project(t, rm) == name]
    return list(reversed(rows))


def task_detail(tid: str) -> dict:
    t = hub.load_task(tid)
    d = {"meta": dict(t.meta), "sections": {s: t.section(s) for s in SECTIONS}, "diff": ""}
    repo, base, branch = hub.expand(t.get("repo") or "~/agent-hub"), t.get("base"), t.get("branch")
    diff = ""
    if (repo / ".git").exists():
        if base and branch and hub.git("rev-parse", "--verify", "--quiet", branch, cwd=repo, check=False).returncode == 0:
            diff = hub.git("diff", f"{base}..{branch}", cwd=repo, check=False).stdout
        elif t.get("merged"):  # 반영된 작업: 반영 커밋이 가져온 변경
            diff = hub.git("diff", f"{t.get('merged')}^1", t.get("merged"), cwd=repo, check=False).stdout
    d["diff"] = diff[:80000] + ("\n… (길어서 생략)" if len(diff) > 80000 else "")
    return d


def capabilities() -> dict:
    caps = hub.load_caps()
    cfg = hub.load_cfg()
    return {"auto": caps.get("auto", {}), "manual": caps.get("agents", {}), "type_defaults": caps.get("type_defaults", {}),
            "protected_paths": cfg["protected_paths"], "verify_allowed": cfg.get("verify_allowed", [])}


# ---------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "agent-hub"

    def log_message(self, fmt, *args):  # 접속 로그는 남기지 않는다(조용히)
        pass

    # -- 응답
    def send_json(self, data, code: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, msg: str, code: int = 400) -> None:
        self.send_json({"error": msg}, code)

    # -- 인증
    def session(self) -> str | None:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["hubsess"].value if "hubsess" in c else None
        if tok and SESSIONS.get(tok, 0) > time.time():
            return tok
        return None

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            raise ValueError("요청이 너무 큼")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # -- 라우팅
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not path.startswith("/api/"):
            return self.fail("없는 주소", 404)
        if not self.session():
            return self.fail("로그인 필요", 401)
        try:
            if path == "/api/overview":
                return self.send_json(overview())
            if m := re.fullmatch(r"/api/projects/([a-z0-9-]+)/tasks", path):
                return self.send_json({"tasks": project_tasks(m.group(1))})
            if m := re.fullmatch(r"/api/tasks/(T-\d+)", path):
                return self.send_json(task_detail(m.group(1)))
            if m := re.fullmatch(r"/api/jobs/([0-9a-f]+)", path):
                job = JOBS.get(m.group(1))
                return self.send_json(job) if job else self.fail("없는 작업", 404)
            if path == "/api/capabilities":
                return self.send_json(capabilities())
        except SystemExit as e:
            return self.fail(str(e), 404)
        except Exception as e:
            return self.fail(f"서버 오류: {e}", 500)
        return self.fail("없는 주소", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if self.headers.get("X-Hub") != "1":  # 다른 사이트에서 몰래 보내는 요청 차단
            return self.fail("잘못된 요청", 403)
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError) as e:
            return self.fail(str(e))
        if path == "/api/login":
            if check_password(str(data.get("password", ""))):
                tok = secrets.token_urlsafe(32)
                SESSIONS[tok] = time.time() + SESSION_DAYS * 86400
                ck = f"hubsess={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_DAYS * 86400}"
                return self.send_json({"ok": True}, headers={"Set-Cookie": ck})
            time.sleep(1.5)
            return self.fail("비밀번호가 틀렸습니다", 401)
        tok = self.session()
        if not tok:
            return self.fail("로그인 필요", 401)
        try:
            return self.route_post(path, data, tok)
        except SystemExit as e:
            return self.fail(str(e), 404)
        except Exception as e:
            return self.fail(f"서버 오류: {e}", 500)

    def route_post(self, path: str, data: dict, tok: str):
        if path == "/api/logout":
            SESSIONS.pop(tok, None)
            return self.send_json({"ok": True})

        if path == "/api/projects":  # 새 프로젝트: 저장소 생성 → 계획 → (바로 시작이면) 실행
            name = str(data.get("name", "")).strip().lower()
            goal = str(data.get("goal", "")).strip()
            auto = bool(data.get("autostart"))
            if not hub.NAME_RE.match(name):
                return self.fail("이름은 영어 소문자·숫자·하이픈(-)만, 2~39자")
            if not goal:
                return self.fail("무엇을 만들지 목표를 적어 주세요")

            def job(say):
                hub.create_project(name, goal.splitlines()[0][:200], say)
                say("Claude가 작업 계획을 세우는 중(1~3분)")
                tasks = hub.propose_plan(name, goal)
                say(f"작업 {len(tasks)}개 계획됨")
                if auto:
                    ids = hub.start_plan(name, tasks)
                    kick()
                    say(f"바로 시작: {', '.join(ids)}")
                    return {"project": name, "started": ids}
                return {"project": name, "plan": tasks}

            return self.send_json({"job": start_job(f"새 프로젝트 {name}", job)})

        if m := re.fullmatch(r"/api/projects/([a-z0-9-]+)/(propose|start|ask|settings)", path):
            name, action = m.group(1), m.group(2)
            if name not in hub.load_projects():
                return self.fail("없는 프로젝트", 404)
            if action == "propose":
                goal = str(data.get("goal", "")).strip()
                if not goal:
                    return self.fail("목표를 적어 주세요")

                def job(say):
                    say("Claude가 작업 계획을 세우는 중(1~3분)")
                    tasks = hub.propose_plan(name, goal)
                    say(f"작업 {len(tasks)}개 계획됨")
                    return {"project": name, "plan": tasks}

                return self.send_json({"job": start_job(f"{name} 계획", job)})
            if action == "start":
                tasks = data.get("tasks") or []
                if not isinstance(tasks, list) or not tasks:
                    return self.fail("시작할 작업이 없습니다")
                ids = hub.start_plan(name, [t for t in tasks if isinstance(t, dict) and t.get("title")][:10])
                kick()
                return self.send_json({"started": ids})
            if action == "ask":
                text = str(data.get("text", "")).strip()
                if not text:
                    return self.fail("내용을 적어 주세요")
                proj = hub.load_projects()[name]
                t = hub.create_task(text.splitlines()[0][:60], proj["repo"], text, status="실행요청",
                                    origin="web", project=name)
                kick()
                return self.send_json({"id": t.id})
            if action == "settings":
                projects = hub.load_projects()
                if "auto_merge" in data:
                    projects[name]["auto_merge"] = bool(data["auto_merge"])
                hub.save_projects(projects, f"{name} 설정 변경")
                return self.send_json({"ok": True})

        if m := re.fullmatch(r"/api/tasks/(T-\d+)/(merge|retry|cancel)", path):
            tid, action = m.group(1), m.group(2)
            hub.hub_pull()
            t = hub.load_task(tid)
            st = t.get("status")
            if action == "merge":
                ok, msg = hub.merge_task(t, hub.load_cfg())
                if ok:
                    kick()  # 이 작업을 기다리던 다음 작업이 있으면 시작
                return self.send_json({"ok": ok, "message": msg})
            if action == "retry":
                if st not in EDITABLE:
                    return self.fail(f"상태가 '{st}'라서 다시 맡길 수 없음")
                extra = str(data.get("text", "")).strip()
                if extra:
                    t.append("목표", f"추가 요청({hub.now()}): {extra}")
                for k in ("stage", "round", "retry_after", "auto_retries"):
                    t.set(k, "")
                t.set("status", "실행요청")
                hub.sync(t, "다시 맡김")
                kick()
                return self.send_json({"ok": True})
            if action == "cancel":
                if st == "진행":
                    return self.fail("실행 중인 작업은 끝난 뒤 취소할 수 있음")
                t.set("status", "취소")
                hub.sync(t, "취소")
                return self.send_json({"ok": True})

        if path == "/api/capabilities/refresh":
            def job(say):
                say("설치된 스킬·플러그인·MCP 수집 중")
                p = subprocess.run([sys.executable, str(HUB_DIR / "scripts" / "hub.py"), "caps"], cwd=str(HUB_DIR),
                                   capture_output=True, text=True, timeout=300)
                say("완료" if p.returncode == 0 else f"실패: {p.stderr[-300:]}")
                return {}

            return self.send_json({"job": start_job("능력 목록 수집", job)})

        return self.fail("없는 주소", 404)


def serve(host: str, port: int) -> int:
    if not AUTH.exists():
        print("비밀번호가 없습니다. 먼저: python3 web/server.py passwd")
        return 1
    if host in ("0.0.0.0", "::", ""):
        print("모든 주소에 여는 것은 막혀 있습니다. Tailscale 주소(100.x.x.x)를 --host 로 주세요")
        return 1
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"agent-hub 웹: http://{host}:{port}", flush=True)
    httpd.serve_forever()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("passwd")
    p = sub.add_parser("serve")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=hub.load_cfg().get("web", {}).get("port", 8787))
    a = ap.parse_args()
    return set_password() if a.cmd == "passwd" else serve(a.host, a.port)


if __name__ == "__main__":
    sys.exit(main())
