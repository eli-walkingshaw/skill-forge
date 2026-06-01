"""Capture context from the user's environment.

All grabs are best-effort. If something fails, the field is empty and capture
proceeds. Nothing here is allowed to crash the capture flow.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class CaptureContext:
    """Auto-grabbed context that surrounds a capture."""
    timestamp: str
    clipboard: str = ""
    shell_history: list[str] = field(default_factory=list)
    last_claude_exchange: str = ""
    last_claude_source: str = ""  # path/id of the session it came from
    active_app: str = ""

    def to_prompt_block(self) -> str:
        """Render the context as a markdown block for the API."""
        parts = ["## Surrounding context (auto-grabbed)\n"]
        if self.active_app:
            parts.append(f"**Active app at capture time:** {self.active_app}\n")
        if self.clipboard:
            parts.append(f"**Clipboard:**\n```\n{self.clipboard[:1500]}\n```\n")
        if self.shell_history:
            joined = "\n".join(self.shell_history[-10:])
            parts.append(f"**Last shell commands:**\n```\n{joined}\n```\n")
        if self.last_claude_exchange:
            parts.append(
                f"**Last Claude exchange** (from {self.last_claude_source}):\n"
                f"```\n{self.last_claude_exchange[:1500]}\n```\n"
            )
        if len(parts) == 1:
            return "## Surrounding context: _none captured_\n"
        return "\n".join(parts)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def grab_clipboard() -> str:
    """Read the clipboard via macOS `pbpaste`. Empty string on any failure."""
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def grab_shell_history(n: int = 10) -> list[str]:
    """Read the last N commands from ~/.zsh_history.

    Zsh history has a weird extended format: `: <timestamp>:<elapsed>;<command>`
    when EXTENDED_HISTORY is on (default in oh-my-zsh). We strip that prefix.
    Plain history is just one command per line.
    """
    histfile = Path(os.environ.get("HISTFILE") or "~/.zsh_history").expanduser()
    if not histfile.exists():
        # Try bash as a fallback.
        histfile = Path("~/.bash_history").expanduser()
        if not histfile.exists():
            return []

    try:
        # Zsh history is sometimes not UTF-8; ignore decode errors.
        text = histfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    raw_lines = [l for l in text.splitlines() if l.strip()]
    cleaned = []
    for line in raw_lines[-n * 3:]:  # over-fetch since some may be junk
        # Strip the extended-history prefix.
        m = re.match(r"^:\s*\d+:\d+;(.*)$", line)
        cmd = m.group(1) if m else line
        cmd = cmd.strip()
        # Skip things that look like trash (continuation lines start with whitespace).
        if not cmd or cmd.startswith("#"):
            continue
        cleaned.append(cmd)
    return cleaned[-n:]


def grab_last_claude_exchange(
    logs_path: Path,
    within_minutes: int = 120,
) -> tuple[str, str]:
    """Find the most recent Claude Code session and return its last user→assistant exchange.

    Returns (exchange_text, source_ref) — both empty strings if nothing found.
    """
    if not logs_path.exists():
        return "", ""

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    most_recent: tuple[Path, float] | None = None

    for jsonl in logs_path.rglob("*.jsonl"):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        if mtime_dt < cutoff:
            continue
        if most_recent is None or mtime > most_recent[1]:
            most_recent = (jsonl, mtime)

    if most_recent is None:
        return "", ""

    session_path, _ = most_recent
    try:
        lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""

    last_user: dict | None = None
    last_assistant_chunks: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("type") or obj.get("role")
        if role == "user":
            last_user = obj
            last_assistant_chunks = []  # reset; we want the LAST user→assistant pair
        elif role == "assistant" and last_user is not None:
            last_assistant_chunks.append(_extract_text(obj))

    if last_user is None:
        return "", ""

    user_text = _extract_text(last_user)
    assistant_text = "\n".join(last_assistant_chunks).strip()
    if not user_text and not assistant_text:
        return "", ""

    formatted = f"USER: {user_text[:600]}\n\nASSISTANT: {assistant_text[:1000]}"
    return formatted, session_path.name


def _extract_text(msg: dict) -> str:
    """Pull text from a Claude Code message, regardless of content shape."""
    content = msg.get("content") or msg.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def grab_active_app() -> str:
    """Best-effort: which app is frontmost via AppleScript. Empty on failure."""
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to '
                'name of first application process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def collect_context(claude_logs_path: Path) -> CaptureContext:
    """Run every grabber, swallow errors, return a complete context."""
    last_exchange, last_source = grab_last_claude_exchange(claude_logs_path)
    return CaptureContext(
        timestamp=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        clipboard=grab_clipboard(),
        shell_history=grab_shell_history(),
        last_claude_exchange=last_exchange,
        last_claude_source=last_source,
        active_app=grab_active_app(),
    )

