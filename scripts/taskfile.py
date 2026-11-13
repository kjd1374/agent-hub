"""작업 파일(tasks/T-번호.md) 읽기·쓰기.

맨 위 --- 사이는 `키: 값` 한 줄씩(메타), 그 아래는 `## 제목` 섹션들.
"""
from __future__ import annotations

import re
from pathlib import Path

_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


class Task:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.load()

    # ---- 읽기 -------------------------------------------------------------
    def load(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        m = _FM.match(text)
        if not m:
            raise ValueError(f"{self.path.name}: 맨 위에 --- 로 감싼 메타 영역이 없습니다")
        self.meta: dict[str, str] = {}
        self.order: list[str] = []
        for line in m.group(1).splitlines():
            if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip()
            if k not in self.meta:
                self.order.append(k)
            self.meta[k] = v.strip()
        self.body = text[m.end():]

    @property
    def id(self) -> str:
        return self.get("id") or self.path.stem

    def get(self, key: str, default: str = "") -> str:
        v = self.meta.get(key, "")
        return v if v else default

    def needs(self) -> list[str]:
        return [n.strip() for n in self.get("needs").split(",") if n.strip()]

    def section(self, name: str) -> str:
        """`## name` 아래 내용(다음 `## ` 전까지). 주석(<!-- -->)과 앞뒤 빈 줄은 뺀다."""
        start, end = self._span(name)
        if start is None:
            return ""
        text = self.body[start:end]
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        return text.strip()

    # ---- 쓰기 -------------------------------------------------------------
    def set(self, key: str, value) -> None:
        if key not in self.meta:
            self.order.append(key)
        self.meta[key] = str(value).replace("\n", " ").strip()

    def append(self, name: str, text: str) -> None:
        """섹션 끝에 내용을 덧붙인다. 섹션이 없으면 맨 끝에 만든다."""
        start, end = self._span(name)
        if start is None:
            self.body = self.body.rstrip() + f"\n\n## {name}\n\n{text.rstrip()}\n"
            return
        current = self.body[start:end].strip("\n")
        self._put(start, end, (current + "\n" if current.strip() else "") + text.rstrip())

    def replace_section(self, name: str, text: str) -> None:
        start, end = self._span(name)
        if start is None:
            self.body = self.body.rstrip() + f"\n\n## {name}\n\n{text.rstrip()}\n"
            return
        self._put(start, end, text.rstrip())

    def _put(self, start: int, end: int, content: str) -> None:
        rest = self.body[end:]
        self.body = self.body[:start] + "\n\n" + content + "\n" + ("\n" + rest if rest else "")

    def save(self) -> None:
        head = "\n".join(f"{k}: {self.meta[k]}" for k in self.order)
        body = self.body if self.body.startswith("\n") else "\n" + self.body
        self.path.write_text(f"---\n{head}\n---{body.rstrip()}\n", encoding="utf-8")

    # ---- 내부 -------------------------------------------------------------
    def _span(self, name: str):
        """섹션 내용의 [시작, 끝) 위치. 시작은 제목 줄 다음."""
        m = re.search(rf"^## {re.escape(name)}[ \t]*$", self.body, flags=re.M)
        if not m:
            return None, None
        start = m.end()
        n = re.search(r"^## ", self.body[start:], flags=re.M)
        end = start + n.start() if n else len(self.body)
        return start, end
