"""Capture: a single observed task entry."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass
class Capture:
    """One observed task — user goal + what was done about it."""
    id: str                       # stable hash
    source: str                   # "claude-code" | "inbox" | "claude-ai"
    source_ref: str               # file path, session id, etc.
    timestamp: str                # ISO 8601
    goal: str                     # high-level task summary
    pattern: str                  # the reusable fix/approach
    tools: list[str] = field(default_factory=list)  # e.g. ["SuiteScript", "Rhino"]
    raw_excerpt: str = ""         # for the prompt, truncated

    @classmethod
    def make(
        cls,
        *,
        source: str,
        source_ref: str,
        timestamp: str,
        goal: str,
        pattern: str,
        tools: list[str] | None = None,
        raw_excerpt: str = "",
    ) -> "Capture":
        # Stable ID: hash of (source, source_ref, goal). If the same task
        # surfaces twice from the same source, we dedupe.
        h = hashlib.sha256(f"{source}|{source_ref}|{goal}".encode("utf-8")).hexdigest()[:16]
        return cls(
            id=h,
            source=source,
            source_ref=source_ref,
            timestamp=timestamp,
            goal=goal.strip(),
            pattern=pattern.strip(),
            tools=tools or [],
            raw_excerpt=raw_excerpt[:2000],
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Capture":
        return cls(**json.loads(line))


def write_captures(captures: Iterable[Capture], path: Path) -> int:
    """Append captures to the JSONL store, deduping by id."""
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_ids: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    existing_ids.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    written = 0
    with path.open("a", encoding="utf-8") as f:
        for c in captures:
            if c.id in existing_ids:
                continue
            f.write(c.to_json() + "\n")
            existing_ids.add(c.id)
            written += 1
    return written


def read_captures(path: Path) -> list[Capture]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(Capture.from_json(line))
            except (json.JSONDecodeError, TypeError):
                continue
    return out


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
