"""Source readers: turn raw inputs into Capture objects.

Each source has a `read(config)` function returning an iterable of Captures.
A source is intentionally lossy: it scans for task-shaped exchanges and
extracts a (goal, pattern) pair. Noise is dropped here, not later.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .capture import Capture, now_iso
from .config import Config


# ---------- Inbox source ----------------------------------------------------

INBOX_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL
)


def read_inbox(config: Config) -> Iterable[Capture]:
    """Read manual notes from vault/inbox/*.md.

    Format (frontmatter optional):

        ---
        goal: Fix Suitelet white screen on save
        tools: [SuiteScript, Rhino]
        ---
        I had to percent-encode the # in the SVG data URI...

    Without frontmatter, the first heading is the goal and the body is
    the pattern.
    """
    inbox = config.inbox_dir
    if not inbox.exists():
        return

    for md in sorted(inbox.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue

        goal, pattern, tools = _parse_inbox_note(text, fallback_goal=md.stem)
        if not pattern.strip():
            continue

        ts = datetime.fromtimestamp(md.stat().st_mtime, tz=timezone.utc)
        yield Capture.make(
            source="inbox",
            source_ref=str(md),
            timestamp=ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
            goal=goal,
            pattern=pattern,
            tools=tools,
            raw_excerpt=text,
        )


def _parse_inbox_note(text: str, fallback_goal: str) -> tuple[str, str, list[str]]:
    m = INBOX_FRONTMATTER_RE.match(text)
    if m:
        fm_raw, body = m.group(1), m.group(2)
        fm = _parse_simple_yaml(fm_raw)
        goal = str(fm.get("goal") or fallback_goal)
        tools = fm.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.strip("[]").split(",") if t.strip()]
        return goal, body.strip(), tools

    # No frontmatter — first H1/H2 line is the goal.
    lines = text.strip().splitlines()
    goal = fallback_goal
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("# ") or s.startswith("## "):
            goal = s.lstrip("#").strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    return goal, body, []


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser — handles flat key: value pairs and simple lists."""
    out: dict = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


# ---------- Claude Code source ----------------------------------------------


def read_claude_code(config: Config) -> Iterable[Capture]:
    """Scan Claude Code session JSONL logs for task-shaped exchanges.

    Claude Code stores sessions under ~/.claude/projects/<project>/sessions/<id>.jsonl
    (path varies by version; we glob for *.jsonl under the configured root).

    Heuristic: a session "task" is the first substantive user message plus
    the assistant turns that follow until the next user message. We treat that
    block as a (goal, pattern) candidate and let clustering decide if it
    repeats elsewhere.
    """
    root = config.claude_code_logs_path
    if not root.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=config.scan_days_back)

    for jsonl in root.rglob("*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue

        yield from _scan_session_jsonl(jsonl)


def _scan_session_jsonl(path: Path) -> Iterable[Capture]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    # Pair up user messages with the following assistant block.
    pending_user: dict | None = None
    assistant_buf: list[str] = []

    def flush() -> Capture | None:
        if not pending_user or not assistant_buf:
            return None
        user_text = _extract_text(pending_user)
        if _is_noise(user_text):
            return None  # Shell paste / banner / too short.
        assistant_text = "\n".join(assistant_buf)
        goal = _first_sentence(user_text, max_len=120)
        pattern = _summarize_assistant(assistant_text)
        if not pattern:
            return None
        tools = _detect_tools(user_text + " " + assistant_text)
        ts = pending_user.get("timestamp") or now_iso()
        return Capture.make(
            source="claude-code",
            source_ref=f"{path.name}#{pending_user.get('uuid', '')}",
            timestamp=ts,
            goal=goal,
            pattern=pattern,
            tools=tools,
            raw_excerpt=(user_text + "\n---\n" + assistant_text)[:2000],
        )

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = obj.get("type") or obj.get("role")
        if role == "user":
            cap = flush()
            if cap:
                yield cap
            pending_user = obj
            assistant_buf = []
        elif role == "assistant" and pending_user:
            assistant_buf.append(_extract_text(obj))

    cap = flush()
    if cap:
        yield cap


def _extract_text(msg: dict) -> str:
    """Pull text out of a session log message, regardless of shape."""
    content = msg.get("content") or msg.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}]")
        return "\n".join(parts)
    return ""


def _first_sentence(text: str, max_len: int = 120) -> str:
    text = text.strip().replace("\n", " ")
    for sep in [". ", "? ", "! ", "\n"]:
        if sep in text[:max_len * 2]:
            text = text.split(sep, 1)[0]
            break
    return text[:max_len].rstrip()


def _summarize_assistant(text: str) -> str:
    """Pull the most pattern-like chunk: code blocks, fixes, conclusions."""
    # Prefer the first fenced code block + its preceding sentence.
    code_match = re.search(r"([^\n]+?)\n```[\w-]*\n(.+?)\n```", text, re.DOTALL)
    if code_match:
        lead = code_match.group(1).strip()
        code = code_match.group(2).strip()
        snippet = f"{lead}\n```\n{code[:400]}\n```"
        return snippet[:600]
    # Otherwise, take the first ~3 substantive lines.
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return " ".join(lines[:3])[:400]



SHELL_PROMPT_RE = re.compile(
    r"^(\([^)]+\)\s+)?[\w.-]+@[\w.-]+\s+\S+\s+%",
)
NOISE_PREFIXES = (
    "this session is being continued",
    "caveat: the messages below were generated",
    "<command-name>",
    "<local-command-stdout>",
    "<bash-input>",
    "<bash-stdout>",
)

ERROR_LIKE_RE = re.compile(
    r"^(error|exception|traceback|fatal|warning|fail(ed|ure)?|"
    r"\w+error:|\w+exception:|\[error\])",
    re.IGNORECASE,
)
HELP_SEEKING_RE = re.compile(
    r"\b(help|fix|debug|why|what'?s wrong|getting (this|an) error|"
    r"how (do|can|should) i|can you|please|stuck|broken|not working)\b",
    re.IGNORECASE,
)

def _is_noise(text: str) -> bool:
    """Return True if text looks like terminal output, banners, help-seeking, or paste-ins.

    The goal here is: only let through messages that describe a PATTERN
    ("I solved X by doing Y"), not messages that ASK for a pattern
    ("X is broken, help"). Session logs are full of the latter.
    """
    t = text.strip()
    if not t or len(t) < 30:
        return True
    low = t.lower()
    if any(low.startswith(p) for p in NOISE_PREFIXES):
        return True
    first_line = t.splitlines()[0].strip()
    if SHELL_PROMPT_RE.match(first_line):
        return True
    if ERROR_LIKE_RE.match(first_line):
        return True
    if re.match(r"^(cd|ls|pwd|git|brew|npm|pip|python3?)\s", t) and len(t) < 200:
        return True
    # Mostly help-seeking and short: skip. (Long messages can still be
    # informative even if they contain help-seeking words, so we only filter
    # when the message is short enough that it's PROBABLY just a question.)
    if len(t) < 300 and HELP_SEEKING_RE.search(low):
        return True
    return False

TOOL_PATTERNS = {
    "SuiteScript": re.compile(r"\b(suitescript|suitelet|netsuite)\b", re.I),
    "Rhino/ES5": re.compile(r"\b(rhino|es5|backtick|template literal)\b", re.I),
    "Next.js": re.compile(r"\b(next\.?js|app router|middleware\.ts)\b", re.I),
    "React": re.compile(r"\b(react|jsx|usestate|useeffect)\b", re.I),
    "Prisma": re.compile(r"\bprisma\b", re.I),
    "Zod": re.compile(r"\bzod\b", re.I),
    "Slack": re.compile(r"\bslack\b", re.I),
    "Obsidian": re.compile(r"\bobsidian\b", re.I),
    "Git": re.compile(r"\b(git push|git commit|git pull|merge conflict)\b", re.I),
    "Python": re.compile(r"\b(python|\.py|pip install)\b", re.I),
    "TypeScript": re.compile(r"\b(typescript|\.ts |\.tsx)\b", re.I),
}


def _detect_tools(text: str) -> list[str]:
    return [name for name, pat in TOOL_PATTERNS.items() if pat.search(text)]


# ---------- Claude.ai export source -----------------------------------------


def read_claude_ai_exports(config: Config) -> Iterable[Capture]:
    """Read claude.ai conversation exports dropped into vault/inbox/*.json.

    The export format is a JSON array of messages; we use the same pairing
    logic as Claude Code logs.
    """
    inbox = config.inbox_dir
    if not inbox.exists():
        return

    for jf in sorted(inbox.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        messages = data if isinstance(data, list) else data.get("chat_messages") or data.get("messages")
        if not isinstance(messages, list):
            continue

        pending_user = None
        assistant_buf: list[str] = []

        def make_capture():
            if not pending_user or not assistant_buf:
                return None
            user_text = _extract_text(pending_user)
            if _is_noise(user_text):
                return None
            assistant_text = "\n".join(assistant_buf)
            return Capture.make(
                source="claude-ai",
                source_ref=f"{jf.name}#{pending_user.get('uuid', pending_user.get('id', ''))}",
                timestamp=pending_user.get("created_at") or pending_user.get("timestamp") or now_iso(),
                goal=_first_sentence(user_text),
                pattern=_summarize_assistant(assistant_text),
                tools=_detect_tools(user_text + " " + assistant_text),
                raw_excerpt=(user_text + "\n---\n" + assistant_text)[:2000],
            )

        for m in messages:
            role = m.get("sender") or m.get("role")
            if role in ("human", "user"):
                cap = make_capture()
                if cap:
                    yield cap
                pending_user = m
                assistant_buf = []
            elif role in ("assistant",) and pending_user:
                assistant_buf.append(_extract_text(m))

        cap = make_capture()
        if cap:
            yield cap


# ---------- Dispatch --------------------------------------------------------


SOURCE_FUNCS = {
    "inbox": read_inbox,
    "claude-code": read_claude_code,
    "claude-ai-exports": read_claude_ai_exports,
}


def read_all(config: Config) -> list[Capture]:
    """Run every configured source and collect captures."""
    out: list[Capture] = []
    for src in config.sources:
        fn = SOURCE_FUNCS.get(src)
        if not fn:
            print(f"[warn] unknown source: {src}")
            continue
        out.extend(fn(config))
    return out
