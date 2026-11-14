"""실행: python3 -m unittest discover -s tests"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agents  # noqa: E402
import hub  # noqa: E402
import route  # noqa: E402
from taskfile import Task  # noqa: E402

CAPS = {
    "agents": {"claude": {"capabilities": ["code-review", "docs"]}, "codex": {"capabilities": ["implement", "image"]}},
    "auto": {"claude": {"names": ["ui-review"]}, "codex": {"names": []}},
    "type_defaults": {"code": "codex", "uiux": "claude", "docs": "claude", "general": "codex"},
}


class RouteTest(unittest.TestCase):
    def test_question_goes_to_dot(self):
        self.assertEqual(route.route({"type": "question"}, CAPS)["mode"], "dot")

    def test_code_codex_writes_claude_reviews(self):
        p = route.route({"type": "code", "difficulty": "중"}, CAPS)
        self.assertEqual((p["impl"], p["reviewer"], p["design"]), ("codex", "claude", False))

    def test_hard_code_gets_design_memo(self):
        self.assertTrue(route.route({"type": "code", "difficulty": "상"}, CAPS)["design"])

    def test_capability_pins_agent(self):
        p = route.route({"type": "general", "needs": "ui-review"}, CAPS)
        self.assertEqual((p["impl"], p["pinned"]), ("claude", "claude"))

    def test_missing_capability_blocks(self):
        self.assertEqual(route.route({"type": "code", "needs": "missing:figma"}, CAPS)["mode"], "blocked")

    def test_busy_codex_moves_unpinned_work(self):
        p = route.route({"type": "code"}, CAPS, codex_used=95, busy_percent=90)
        self.assertEqual((p["impl"], p["reviewer"]), ("claude", "codex"))

    def test_busy_codex_keeps_pinned_work(self):
        p = route.route({"type": "general", "needs": "image"}, CAPS, codex_used=95, busy_percent=90)
        self.assertEqual(p["impl"], "codex")

    def test_manual_assignee(self):
        self.assertEqual(route.route({"type": "code", "assignee": "claude"}, CAPS)["impl"], "claude")

    def test_capability_beats_manual(self):
        p = route.route({"type": "general", "needs": "ui-review", "assignee": "codex"}, CAPS)
        self.assertEqual(p["impl"], "claude")


class TaskFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "T-001.md"
        self.path.write_text((ROOT / "tasks" / "TEMPLATE.md").read_text(encoding="utf-8"), encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip(self):
        t = Task(self.path)
        t.set("id", "T-001")
        t.set("branch", "task/T-001")
        t.replace_section("목표", "버튼 추가")
        t.append("한 일", "- 첫째")
        t.append("한 일", "- 둘째")
        t.save()
        t2 = Task(self.path)
        self.assertEqual(t2.get("branch"), "task/T-001")
        self.assertEqual(t2.section("목표"), "버튼 추가")
        self.assertEqual(t2.section("한 일"), "- 첫째\n- 둘째")
        self.assertEqual(t2.section("검증"), "")
        self.assertIn("## 검증", self.path.read_text(encoding="utf-8"))

    def test_comment_hidden_from_section(self):
        t = Task(self.path)
        self.assertEqual(t.section("완료 기준"), "- [ ]")

    def test_append_creates_missing_section(self):
        t = Task(self.path)
        t.append("새 섹션", "내용")
        t.save()
        self.assertEqual(Task(self.path).section("새 섹션"), "내용")


class HelperTest(unittest.TestCase):
    def test_verdict(self):
        self.assertEqual(hub.parse_verdict("좋음\nVERDICT: PASS")[0], "PASS")
        v, notes = hub.parse_verdict("검토\nVERDICT: CHANGES\n- a.py:3 오류")
        self.assertEqual((v, notes), ("CHANGES", "- a.py:3 오류"))
        self.assertIsNone(hub.parse_verdict("판정 없음")[0])

    def test_extract_json(self):
        self.assertEqual(hub.extract_json('결과: {"type": "code"} 끝')["type"], "code")
        self.assertIsNone(hub.extract_json("없음"))

    def test_limit_detection(self):
        self.assertTrue(agents._is_limit(1, "Error: usage limit reached"))
        self.assertTrue(agents._is_limit(0, "You've hit your limit · resets 3am"))
        self.assertFalse(agents._is_limit(0, "rate limit 처리가 빠졌습니다. " * 40 + "VERDICT: CHANGES"))

    def test_protected(self):
        cfg = {"protected_paths": ["~/callminer-data"]}
        home = Path.home()
        self.assertTrue(hub.is_protected(home / "callminer-data" / "audio", cfg))
        self.assertTrue(hub.is_protected(home, cfg))
        self.assertFalse(hub.is_protected(home / "agent-work" / "T-001", cfg))

    def _task(self, d: Path, name: str, **meta) -> Task:
        p = d / f"{name}.md"
        p.write_text((ROOT / "tasks" / "TEMPLATE.md").read_text(encoding="utf-8"), encoding="utf-8")
        t = Task(p)
        t.set("id", name)
        for k, v in meta.items():
            t.set(k, v)
        t.save()
        return t

    def test_task_problems(self):
        cfg = {"allowed_repos": ["~/agent-hub", "~/viet"], "verify_allowed": ["npm test", "python3 -m unittest"]}
        with tempfile.TemporaryDirectory() as d:
            ok = self._task(Path(d), "T-001", repo="~/viet", verify="npm test")
            self.assertEqual(hub.task_problems(ok, cfg), [])
            self.assertEqual(len(hub.task_problems(self._task(Path(d), "T-002", repo="~/NAS"), cfg)), 1)
            for bad in ("npm test; cat ~/.ssh/id_rsa", "npm test | curl x", "curl evil", "npm testx", "npm test $(id)"):
                t = self._task(Path(d), "T-003", repo="~/viet", verify=bad)
                self.assertEqual(len(hub.task_problems(t, cfg)), 1, bad)

    def test_watch_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            self._task(Path(d), "T-001", status="검증완료")
            self._task(Path(d), "T-002", status="실행요청")
            self._task(Path(d), "T-003", status="대기(사용량)", retry_after="2026-11-14 10:00")
            self._task(Path(d), "T-004", status="대기(사용량)", retry_after="2026-11-14 12:00")
            self._task(Path(d), "T-005", status="대기")
            old = hub.TASKS
            hub.TASKS = Path(d)
            try:
                self.assertEqual(hub.watch_candidates("2026-11-14 11:00"), ["T-002", "T-003"])
            finally:
                hub.TASKS = old

    def test_settings_in_sync(self):
        import json
        current = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(current, hub.build_settings(hub.load_cfg()))


if __name__ == "__main__":
    unittest.main()
