"""배정 규칙. AI가 아니라 정해진 규칙으로 정한다 → 같은 작업은 항상 같게 배정되고 이유가 남는다.

규칙(위에서부터):
  1. 단순 질문(question) → 실행하지 않고 돌쇠가 답한다.
  2. 필요 능력(needs)이 한 에이전트에만 있으면 그 에이전트로 고정. 아무에게도 없으면 막고 설치 필요라고 알린다.
  3. 작업 파일에서 assignee 를 직접 정했으면 그대로.
  4. 코딩 → Codex 작성 + Claude 검증. 난도 상이면 Claude가 짧은 설계 메모 먼저.
  5. 그 밖의 유형 → capabilities.json 의 type_defaults. 검증은 만든 쪽의 반대.
  6. Codex 사용량이 기준(%) 이상이면, 고정되지 않은 Codex 몫을 Claude로.
"""
from __future__ import annotations

AGENTS = ("claude", "codex")
TYPES = ("code", "design", "uiux", "docs", "bulk", "general", "question")


def other(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def has(agent: str, need: str, caps: dict) -> bool:
    manual = set(caps.get("agents", {}).get(agent, {}).get("capabilities", []))
    found = set(caps.get("auto", {}).get(agent, {}).get("names", []))
    return need in manual or need in found


def route(meta: dict, caps: dict, codex_used: float | None = None, busy_percent: float = 90) -> dict:
    """반환: {mode: run|dot|blocked, impl, reviewer, design, pinned, reasons}"""
    t = (meta.get("type") or "general").strip()
    t = t if t in TYPES else "general"
    diff = (meta.get("difficulty") or "중").strip()
    needs = [n.strip() for n in (meta.get("needs") or "").split(",") if n.strip()]
    manual = (meta.get("assignee") or "auto").strip()
    reasons: list[str] = []
    out = {"mode": "run", "impl": None, "reviewer": None, "design": False, "pinned": None, "reasons": reasons}

    if t == "question":
        reasons.append("단순 질문 → 실행 체계를 거치지 않고 돌쇠가 바로 답함")
        out["mode"] = "dot"
        return out

    pinned = None
    for n in needs:
        owners = [a for a in AGENTS if has(a, n, caps)]
        if not owners:
            reasons.append(f'필요 능력 "{n}"을 가진 에이전트가 없음 → 플러그인·스킬 설치가 필요(승인 후)')
            out["mode"] = "blocked"
            return out
        if len(owners) == 1:
            if pinned and pinned != owners[0]:
                reasons.append(f"필요 능력이 {pinned}와 {owners[0]}에 나뉘어 있음 → 작업을 나눠야 함")
                out["mode"] = "blocked"
                return out
            pinned = owners[0]
            reasons.append(f'"{n}" 능력은 {pinned}에만 있음 → {pinned} 고정')
    if manual in AGENTS:
        if pinned and pinned != manual:
            reasons.append(f"작업 파일은 {manual} 지정이지만 필요 능력 때문에 {pinned} 유지")
        else:
            pinned = manual
            reasons.append(f"작업 파일에서 {manual} 지정")

    codex_busy = codex_used is not None and codex_used >= busy_percent
    if t == "code":
        impl = pinned or "codex"
    else:
        impl = pinned or caps.get("type_defaults", {}).get(t, "codex")
    if impl == "codex" and codex_busy and not pinned:
        impl = "claude"
        reasons.append(f"Codex 사용량 {codex_used:.0f}% ≥ {busy_percent:.0f}% → Claude가 대신 작성")

    out.update(impl=impl, reviewer=other(impl), pinned=pinned)
    if t == "code":
        reasons.append(f"코딩 → {impl} 작성, {other(impl)} 검증")
        if diff == "상" and impl == "codex":
            out["design"] = True
            reasons.append("난도 상 → Claude가 짧은 설계 메모 먼저")
    else:
        reasons.append(f"유형 {t} → {impl} 작성{'' if pinned else '(기본값)'}, {other(impl)} 검증")
    return out
