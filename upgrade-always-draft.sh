#!/usr/bin/env bash
# Always-draft capture upgrade:
# - removes the NOT_A_SKILL refusal path entirely
# - every capture produces a SKILL.md in proposals/, even thin notes
# - thin notes get TODO markers in the body to flesh out
# - draft quality (full vs thin) surfaced in the proposal callout
set -euo pipefail

[[ -f forge/__main__.py ]] || { echo "Run from ~/code/skill-forge"; exit 1; }

mkdir -p .upgrade-always-draft-backup
cp forge/commands.py .upgrade-always-draft-backup/ 2>/dev/null || true
echo "  ✓ backed up forge/commands.py to .upgrade-always-draft-backup/"

cat > forge/commands.py << 'COMMANDS_EOF'
"""Commands that build/edit skills, using existing skills as style reference."""
from __future__ import annotations
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


# ---------- Helpers --------------------------------------------------------


SKILL_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def kebab(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s or "untitled-skill"


def list_installed_skills(config: Config) -> list[Path]:
    """Return paths to all SKILL.md files in the skills repo."""
    repo = config.skills_repo_path
    if not repo.exists():
        return []
    return sorted(repo.glob("*/SKILL.md"))


def read_skill_frontmatter(skill_path: Path) -> dict:
    """Extract just the frontmatter as a dict (best-effort)."""
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def style_reference_block(config: Config, max_skills: int = 10) -> str:
    """Build a prompt block showing the style of existing skills.

    Used as 'use these as examples of how to write SKILL.md descriptions'.
    """
    installed = list_installed_skills(config)
    if not installed:
        return ""

    examples = []
    for p in installed[:max_skills]:
        fm = read_skill_frontmatter(p)
        name = fm.get("name", p.parent.name)
        desc = fm.get("description", "")
        if desc:
            examples.append(f"- **{name}**: {desc}")

    if not examples:
        return ""

    return (
        "## Existing skills (use these as STYLE EXAMPLES — match their tone, "
        "specificity, and use of concrete keywords/file names)\n\n"
        + "\n".join(examples)
    )


def api_call(config: Config, system: str, user: str, max_tokens: int = 4000) -> str:
    """Make a single non-streaming Claude API call. Returns the assembled text."""
    body = {
        "model": config.draft_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": config.anthropic_api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error {e.code}: {err_body[:500]}") from e

    parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


FENCE_WRAP_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)


def strip_outer_fence(text: str) -> str:
    m = FENCE_WRAP_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def wrap_for_obsidian(skill_md: str, header_lines: list[str]) -> str:
    """Wrap a SKILL.md in an Obsidian callout for review."""
    callout = ["> [!info] skill-forge proposal"] + [f"> {l}" for l in header_lines] + [
        ">",
        "> When ready: drag this file to `approved/`. Reject: drag to `archive/`.",
        ">",
        "> The SKILL.md content starts at the next `---` divider.",
        "",
        "---",
        "",
    ]
    return "\n".join(callout) + skill_md


PROPOSAL_DIVIDER_SPLIT_RE = re.compile(r"\n---\n")


def proposal_filename(config: Config, name: str) -> Path:
    date = datetime.utcnow().strftime("%Y-%m-%d")
    return config.proposals_dir / f"{date}__{kebab(name)}.md"


# ---------- forge new ------------------------------------------------------


BLANK_TEMPLATE = """---
name: {name}
description: ONE PARAGRAPH. What this skill does AND when it should trigger. Include concrete keywords, file names, error strings, and user phrases. Be slightly pushy — Claude tends to under-trigger skills.
---

# {title}

One- or two-sentence intro.

## When to use

- Trigger phrase or context 1
- Trigger phrase or context 2
- Specific file names, error strings, or product features that should activate this

## The pattern

The reusable knowledge. Code in fenced blocks. Be concrete.

```
example
```

## Steps

1. Step
2. Step

## Gotchas

- Surprise the user hit the first time
- Past mistake worth not repeating
"""


def cmd_new(args, config: Config) -> int:
    name = kebab(args.name)
    title = " ".join(w.capitalize() for w in name.split("-"))
    config.proposals_dir.mkdir(parents=True, exist_ok=True)

    body = BLANK_TEMPLATE.format(name=name, title=title)
    wrapped = wrap_for_obsidian(
        body,
        [
            f"**new (blank):** `{name}`",
            "**source:** scaffolded template — fill it in",
        ],
    )
    out = proposal_filename(config, name)
    if out.exists() and not args.force:
        print(f"refusing to overwrite {out} (use --force)")
        return 1
    out.write_text(wrapped, encoding="utf-8")
    print(f"✓ {out}")
    print(f"\nOpen it in Obsidian. When ready, drag to approved/.")
    return 0


# ---------- forge draft ----------------------------------------------------


DRAFT_SYSTEM_PROMPT = """You are a skill author for Claude. Given a one-line description of a desired skill, you write a complete SKILL.md file.

SKILL.md structure:

---
name: short-kebab-case-name
description: ONE PARAGRAPH. What the skill does AND when it should trigger. Concrete keywords, file names, error strings, user phrases. Be slightly pushy — Claude tends to under-trigger skills.
---

# Title Case Name

One-to-two sentence intro.

## When to use
Bulleted triggers.

## The pattern
The reusable knowledge. Code in fenced blocks.

## Steps
Numbered actions.

## Gotchas
Surprises and past mistakes.

Rules:
- Keep the file under 200 lines.
- The description controls whether the skill triggers — make it specific.
- Use the user's terminology (project names, tool names) as domain markers.
- If existing skills are provided as style examples, MATCH their tone and specificity.
- Output ONLY the SKILL.md content. No preamble, no surrounding code fences."""


def cmd_draft(args, config: Config) -> int:
    description = " ".join(args.description) if isinstance(args.description, list) else args.description
    if not description.strip():
        print("usage: forge draft \"<one-line description of the skill>\"")
        return 1

    config.proposals_dir.mkdir(parents=True, exist_ok=True)

    style = style_reference_block(config)
    user_msg = f"""Desired skill: {description}

{style}

Write the SKILL.md."""

    print(f"drafting via {config.draft_model}...")
    try:
        raw = api_call(config, DRAFT_SYSTEM_PROMPT, user_msg)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1
    skill_md = strip_outer_fence(raw)

    m = SKILL_NAME_RE.search(skill_md)
    name = m.group(1) if m else kebab(description[:40])

    wrapped = wrap_for_obsidian(
        skill_md,
        [
            f"**drafted:** `{name}`",
            f"**from prompt:** {description}",
            f"**style ref:** {len(list_installed_skills(config))} existing skills",
        ],
    )
    out = proposal_filename(config, name)
    out.write_text(wrapped, encoding="utf-8")
    print(f"✓ {out}")
    print(f"\nOpen it in Obsidian to review. When ready, drag to approved/.")
    return 0


# ---------- forge edit -----------------------------------------------------


def cmd_edit(args, config: Config) -> int:
    name = kebab(args.name)
    skill_path = config.skills_repo_path / name / "SKILL.md"
    if not skill_path.exists():
        # Try a fuzzy match.
        candidates = [p for p in list_installed_skills(config) if name in p.parent.name]
        if not candidates:
            print(f"no installed skill matching '{name}'")
            print(f"  try: forge list")
            return 1
        if len(candidates) > 1:
            print(f"multiple matches:")
            for c in candidates:
                print(f"  {c.parent.name}")
            return 1
        skill_path = candidates[0]
        name = skill_path.parent.name

    config.proposals_dir.mkdir(parents=True, exist_ok=True)
    current = skill_path.read_text(encoding="utf-8")
    wrapped = wrap_for_obsidian(
        current,
        [
            f"**editing:** `{name}` (already installed)",
            f"**source:** {skill_path}",
            ">",
            "> The watcher will OVERWRITE the installed version when you approve this.",
        ],
    )
    out = proposal_filename(config, f"edit-{name}")
    out.write_text(wrapped, encoding="utf-8")
    print(f"✓ {out}")
    print(f"\nEdit it in Obsidian. Approve to overwrite the installed version.")
    return 0


# ---------- forge inbox-to-skill -------------------------------------------


INBOX_TO_SKILL_SYSTEM = """You are a skill author for Claude. The user has written a quick note about something they did. Turn it into a proper SKILL.md.

The note may be terse or just a placeholder. If it's clearly not skill-worthy (e.g. a test, a TODO with no content, just a question), respond with:

NOT_A_SKILL: <one-line reason>

Otherwise, write a complete SKILL.md with frontmatter (name, description), When to use, The pattern, Steps, Gotchas sections.

Use the existing skills (provided below if any) as style examples — match their tone, specificity, and use of concrete keywords.

The description field is critical. It must be specific enough that Claude knows when to trigger this skill. Include named files, error strings, tool names, or user phrases from the note.

Output ONLY the SKILL.md content (or NOT_A_SKILL line). No preamble, no surrounding fences."""


def cmd_inbox_to_skill(args, config: Config) -> int:
    note_path = Path(args.note).expanduser()
    if not note_path.is_absolute():
        # Allow specifying just the filename if it's in inbox/.
        candidate = config.inbox_dir / args.note
        if candidate.exists():
            note_path = candidate
    if not note_path.exists():
        print(f"note not found: {note_path}")
        return 1

    note_text = note_path.read_text(encoding="utf-8")
    style = style_reference_block(config)
    user_msg = f"""## The note

```
{note_text}
```

{style}

Turn this into a SKILL.md, or respond NOT_A_SKILL if it isn't skill-worthy."""

    config.proposals_dir.mkdir(parents=True, exist_ok=True)
    print(f"drafting from {note_path.name} via {config.draft_model}...")
    try:
        raw = api_call(config, INBOX_TO_SKILL_SYSTEM, user_msg)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    if raw.startswith("NOT_A_SKILL"):
        print(raw)
        return 0

    skill_md = strip_outer_fence(raw)
    m = SKILL_NAME_RE.search(skill_md)
    name = m.group(1) if m else kebab(note_path.stem)
    wrapped = wrap_for_obsidian(
        skill_md,
        [
            f"**drafted from inbox note:** `{note_path.name}`",
            f"**proposed name:** `{name}`",
        ],
    )
    out = proposal_filename(config, name)
    out.write_text(wrapped, encoding="utf-8")
    print(f"✓ {out}")
    return 0


# ---------- forge audit ----------------------------------------------------


AUDIT_SYSTEM = """You are reviewing a user's collection of Claude skills. Your job is to identify:

1. **Gaps** — patterns mentioned in recent inbox notes that AREN'T covered by existing skills
2. **Staleness** — existing skills whose descriptions or content reference outdated info (old file paths, deprecated APIs, etc.)
3. **Duplicates** — two or more skills covering substantially overlapping territory
4. **Weak descriptions** — descriptions that won't reliably trigger the skill (vague, missing keywords)

Format your response as four sections (GAPS, STALENESS, DUPLICATES, WEAK DESCRIPTIONS), each with bullet points or "(none found)".

Be specific and short. The user will skim this. Do NOT propose new skills here — just identify the gaps so they can run `forge draft "..."` themselves."""


def cmd_audit(args, config: Config) -> int:
    installed = list_installed_skills(config)
    if not installed:
        print("no installed skills yet")
        return 0

    # Build the user message: full skill files + recent inbox notes.
    skill_blob_parts = []
    for p in installed:
        try:
            content = p.read_text(encoding="utf-8")
            skill_blob_parts.append(f"### {p.parent.name}\n\n{content}")
        except OSError:
            continue
    skills_blob = "\n\n---\n\n".join(skill_blob_parts)

    inbox_notes = []
    if config.inbox_dir.exists():
        for f in sorted(config.inbox_dir.glob("*.md"))[-20:]:  # last 20
            try:
                inbox_notes.append(f"### {f.name}\n\n{f.read_text(encoding='utf-8')}")
            except OSError:
                continue
    inbox_blob = "\n\n---\n\n".join(inbox_notes) if inbox_notes else "_(no inbox notes)_"

    user_msg = f"""## Installed skills ({len(installed)})

{skills_blob}

---

## Recent inbox notes

{inbox_blob}

---

Run the audit."""

    print(f"auditing {len(installed)} skills via {config.draft_model}...\n")
    try:
        result = api_call(config, AUDIT_SYSTEM, user_msg, max_tokens=2000)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1
    print(result)
    return 0


# ---------- forge list -----------------------------------------------------


def cmd_list(args, config: Config) -> int:
    installed = list_installed_skills(config)
    if not installed:
        print("(no skills installed)")
        return 0
    print(f"{len(installed)} installed skill(s):\n")
    for p in installed:
        fm = read_skill_frontmatter(p)
        name = fm.get("name", p.parent.name)
        desc = fm.get("description", "")
        # First sentence of description for compact list view.
        short = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0][:120]
        print(f"  {name}")
        if short:
            print(f"    {short}")
    return 0


# ---------- forge init (kept, simplified) ----------------------------------


CAPTURE_SYSTEM = """You are a skill author for Claude. The user just solved something (or thinks they did) and wrote a short note about it, along with auto-grabbed context from their environment — clipboard, shell history, last Claude exchange, active app.

IMPORTANT: the context is automatically grabbed from whatever they happened to be doing recently. It is OFTEN UNRELATED to the note. Treat each context item with skepticism — only use one if it clearly connects to what the note describes. Silently ignore unrelated context. Do not mention which items you ignored. Do not strain to find a connection that isn't there.

YOUR JOB: always produce a SKILL.md. Even if the note is thin or just a topic without a clear resolution, draft the best skeleton you can, marking gaps explicitly with `_(TODO: fill in)_` so the user knows what to flesh out in review. Drafting a thin starter that the user can finish is more valuable than refusing.

Output format — ALWAYS this shape:

USED_CONTEXT: <comma-separated list of context labels actually used, or "none">
DRAFT_QUALITY: <full | thin>
---
name: short-kebab-case-name
description: ONE PARAGRAPH. What the skill does AND when it should trigger. Concrete keywords from the note (and relevant context only) — file names, error strings, tool names, user phrases. Slightly pushy. If the note is thin, write the best description you can from what's given; mark uncertain parts with `_(TODO: ...)_` inline.
---

# Title Case Name

One-to-two sentence intro.

## When to use
Bulleted triggers, drawn from the note. Be specific. If you don't have enough triggers, write 1-2 and add `_(TODO: add more triggers)_`.

## The pattern
The reusable knowledge. Code blocks if relevant code appeared in the note or in genuinely-related context. If the note doesn't describe a resolution, write `_(TODO: describe the actual fix or pattern here)_` and leave the structure for the user to fill.

## Steps
Numbered actions. If unclear, write `_(TODO: list the steps)_`.

## Gotchas
Surprises and past mistakes inferable from what the user wrote. If none are inferable, write `_(TODO: any surprises worth flagging?)_`.

DRAFT_QUALITY rules:
- `full` = the note had both a problem AND a resolution, and you wrote a complete skill from it. No TODOs needed in the body (description may still have TODOs).
- `thin` = the note was incomplete (just a topic, just a problem, just a question). You drafted what you could and inserted TODOs for the gaps. The user will finish it in review.

Context label vocabulary for USED_CONTEXT (use only labels that appeared and were actually relevant):
- clipboard
- shell-history
- last-claude-exchange
- active-app

Other rules:
- Keep the SKILL.md under 200 lines.
- NEVER invent specific technical details to fill gaps. Use `_(TODO: ...)_` instead.
- If existing skills are provided as style examples, MATCH their tone and specificity.
- The USED_CONTEXT line must come first, then DRAFT_QUALITY on the next line, then a blank line, then the SKILL.md frontmatter. No preamble."""


def cmd_capture(args, config: Config) -> int:
    """Capture a moment: user's note + auto-grabbed context → drafted SKILL.md."""
    from .context import collect_context

    # Resolve the note: --note flag, stdin, or the AppleScript prompt.
    note = ""
    if args.note:
        note = args.note
    elif not sys.stdin.isatty():
        note = sys.stdin.read().strip()
    else:
        # Interactive: pop a macOS dialog asking what they just solved.
        note = _prompt_via_applescript()
        if note is None:
            print("cancelled")
            return 0

    if not note.strip():
        print("✗ no note provided — capture aborted")
        return 1

    print(f"capturing: {note[:80]}{'…' if len(note) > 80 else ''}")
    print("  grabbing context...", end=" ", flush=True)
    ctx = collect_context(config.claude_code_logs_path)
    bits = []
    if ctx.clipboard: bits.append("clipboard")
    if ctx.shell_history: bits.append(f"{len(ctx.shell_history)} shell cmds")
    if ctx.last_claude_exchange: bits.append("last Claude msg")
    if ctx.active_app: bits.append(f"app={ctx.active_app}")
    print(f"got: {', '.join(bits) or '(none)'}")

    config.proposals_dir.mkdir(parents=True, exist_ok=True)
    style = style_reference_block(config)
    user_msg = f"""## What the user wrote

{note}

---

{ctx.to_prompt_block()}

---

{style}

---

Draft the SKILL.md. Always produce one — use TODO placeholders for gaps if the note is thin."""

    print(f"  drafting via {config.draft_model}...")
    try:
        raw = api_call(config, CAPTURE_SYSTEM, user_msg)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    skill_md = strip_outer_fence(raw)

    # The model prefixes output with USED_CONTEXT and DRAFT_QUALITY lines.
    # Pull both, then strip from the skill content.
    used_context = _parse_used_context(skill_md)
    draft_quality = _parse_draft_quality(skill_md)
    skill_md = _strip_header_lines(skill_md)

    m = SKILL_NAME_RE.search(skill_md)
    name = m.group(1) if m else kebab(note[:40])

    quality_label = "thin draft (has TODOs to fill in)" if draft_quality == "thin" else "full draft"

    wrapped = wrap_for_obsidian(
        skill_md,
        [
            f"**captured:** `{name}`",
            f"**draft quality:** {quality_label}",
            f"**note:** {note[:100]}{'…' if len(note) > 100 else ''}",
            f"**context grabbed:** {', '.join(bits) or '(none)'}",
            f"**context used by model:** {used_context or '(none — note was self-contained)'}",
        ],
    )
    out = proposal_filename(config, name, gist=note)
    out.write_text(wrapped, encoding="utf-8")
    print(f"✓ {out}")
    if draft_quality == "thin":
        print(f"  (thin draft — look for TODO markers in Obsidian to flesh out)")
    print(f"\nReview in Obsidian. Drag to approved/ when ready.")
    return 0


_USED_CONTEXT_RE = re.compile(r"^\s*USED_CONTEXT\s*:\s*(.+?)\s*$", re.MULTILINE)
_DRAFT_QUALITY_RE = re.compile(r"^\s*DRAFT_QUALITY\s*:\s*(\w+)\s*$", re.MULTILINE)


def _parse_used_context(text: str) -> str:
    """Pull the USED_CONTEXT line out of the model's response (or '' if absent)."""
    m = _USED_CONTEXT_RE.search(text[:400])
    if not m:
        return ""
    raw = m.group(1).strip()
    if raw.lower() in {"none", "(none)", "n/a"}:
        return ""
    return raw


def _parse_draft_quality(text: str) -> str:
    """Pull the DRAFT_QUALITY line — returns 'full', 'thin', or '' if absent."""
    m = _DRAFT_QUALITY_RE.search(text[:400])
    if not m:
        return ""
    val = m.group(1).strip().lower()
    return val if val in {"full", "thin"} else ""


def _strip_header_lines(text: str) -> str:
    """Remove leading USED_CONTEXT and/or DRAFT_QUALITY header lines, plus
    any blank lines between them and the SKILL.md frontmatter.

    Both headers are optional; we strip whichever are present, in any order.
    """
    lines = text.splitlines()
    i = 0
    n = len(lines)
    saw_header = False
    # Skip leading blank lines defensively
    while i < n and not lines[i].strip():
        i += 1
    # Strip header lines (either order) and any blank lines between them
    while i < n:
        if _USED_CONTEXT_RE.match(lines[i]) or _DRAFT_QUALITY_RE.match(lines[i]):
            saw_header = True
            i += 1
        elif not lines[i].strip() and saw_header:
            # Blank line after a header — skip and keep looking for more headers
            i += 1
        else:
            break
    if not saw_header:
        return text
    result = "\n".join(lines[i:])
    if text.endswith("\n"):
        result += "\n"
    return result


def _prompt_via_applescript() -> str | None:
    """Pop a macOS dialog asking 'what did you just solve?'. Returns None on cancel."""
    script = '''
    set userResponse to display dialog "What did you just solve?" ¬
        default answer "" ¬
        with title "skill-forge capture" ¬
        buttons {"Cancel", "Capture"} ¬
        default button "Capture" ¬
        with icon note
    return text returned of userResponse
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min for user to type
        )
        if result.returncode != 0:
            # User cancelled or AppleScript failed.
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  (popup failed: {e}; fallback: type your note below, then Ctrl+D)")
        try:
            return sys.stdin.read().strip()
        except KeyboardInterrupt:
            return None


# ---------- forge digest ---------------------------------------------------


# Phrases that indicate the user just got a resolution from Claude.
# Used to identify "resolution-shaped" exchanges in session logs.
RESOLUTION_USER_RE = re.compile(
    r"\b("
    r"thanks|thank you|that worked|got it|works now|fixed|nice|perfect|"
    r"great|awesome|that did it|solved|all set|good call|"
    r"that\s*('|’)s\s*it|yep|exactly"
    r")\b",
    re.IGNORECASE,
)

# Phrases that indicate Claude provided a fix in the prior assistant turn.
RESOLUTION_ASSISTANT_RE = re.compile(
    r"\b("
    r"the fix|the issue (is|was)|the problem (is|was)|to fix this|"
    r"the cause (is|was)|you need to|you should|try this|here'?s the fix|"
    r"that should (fix|work|do it)|this should (fix|work|do it)|the solution"
    r")\b",
    re.IGNORECASE,
)


DIGEST_SYSTEM = """You are reviewing a developer's recent Claude Code sessions to spot patterns that might be worth capturing as Claude skills.

You'll receive:
- A list of existing skills (so you don't repeat them)
- A set of resolution-shaped exchanges from the past week (Claude proposed a fix, user confirmed it worked)

Produce a SHORT markdown digest with this structure:

# Weekly skill candidates (week of YYYY-MM-DD)

## Worth capturing
For each genuinely new pattern (not covered by existing skills), one bullet:
- **<short topic>**: 1-sentence summary of what was solved + the gist of the fix. End with the suggested `forge capture` invocation.

## Maybe (worth a glance)
Patterns that might be skill-worthy but you're not sure.

## Already covered
Patterns that map cleanly onto an existing skill (just list the topic + which skill).

Be terse. The user will skim this. Default to fewer items (3-5 total) rather than more. If nothing real was solved this week, say so honestly — the digest is allowed to be empty."""


def cmd_digest(args, config: Config) -> int:
    """Scan recent Claude Code logs for resolution-shaped exchanges, summarize."""
    logs_path = config.claude_code_logs_path
    if not logs_path.exists():
        print(f"no Claude Code logs found at {logs_path}")
        print(f"(CLAUDE_CODE_LOGS_PATH in .env — check the path)")
        return 1

    days = args.days if args.days else 7
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    print(f"scanning last {days} days of Claude Code sessions...")

    resolutions = _find_resolution_exchanges(logs_path, cutoff)
    if not resolutions:
        print("no resolution-shaped exchanges found")
        print("(nothing recent where you said 'thanks' or 'that worked' after Claude proposed a fix)")
        return 0

    print(f"found {len(resolutions)} candidate exchange(s)")

    installed = list_installed_skills(config)
    existing_names = [p.parent.name for p in installed]

    # Build the prompt.
    exchanges_text = "\n\n---\n\n".join(
        f"### Exchange {i+1} ({r['session']}, {r['timestamp']})\n\n"
        f"**Claude said:**\n{r['assistant'][:600]}\n\n"
        f"**User responded:**\n{r['user_confirmation'][:200]}"
        for i, r in enumerate(resolutions[:30])  # cap to avoid blowing the context
    )

    user_msg = f"""## Existing skills ({len(installed)})

{', '.join(existing_names) or '(none)'}

---

## Resolution-shaped exchanges this week ({len(resolutions)} total)

{exchanges_text}

---

Produce the digest."""

    from datetime import date
    print(f"summarizing via {config.draft_model}...")
    try:
        digest_text = api_call(config, DIGEST_SYSTEM, user_msg, max_tokens=2500)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    config.proposals_dir.mkdir(parents=True, exist_ok=True)
    out = config.proposals_dir / f"digest_{date.today().isoformat()}.md"

    wrapped = (
        "> [!info] skill-forge weekly digest\n"
        f"> Generated {date.today().isoformat()} from {len(resolutions)} recent exchanges.\n"
        "> This is a digest of CANDIDATE topics, not a draft. Run `forge capture` on the ones worth keeping.\n"
        "> If a candidate isn't useful, just delete this file.\n\n"
        "---\n\n"
        + digest_text
    )
    out.write_text(wrapped, encoding="utf-8")
    print(f"✓ {out}")
    return 0


def _find_resolution_exchanges(logs_path: Path, cutoff: datetime) -> list[dict]:
    """Walk session logs, return user→assistant→user triples where the final user
    message is a confirmation of a fix.
    """
    found: list[dict] = []
    for jsonl in logs_path.rglob("*.jsonl"):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if datetime.fromtimestamp(mtime, tz=timezone.utc) < cutoff:
            continue

        try:
            lines = jsonl.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        # Walk the session looking for: assistant-with-fix-phrase → user-with-confirmation
        prev_assistant_text = ""
        prev_assistant_ts = ""
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("type") or obj.get("role")
            text = _extract_message_text(obj)
            if role == "assistant":
                if RESOLUTION_ASSISTANT_RE.search(text):
                    prev_assistant_text = text
                    prev_assistant_ts = obj.get("timestamp", "")
                else:
                    prev_assistant_text = ""
            elif role == "user" and prev_assistant_text:
                # Short confirmation messages are the signal (long messages
                # are usually new questions, not confirmations).
                if len(text.strip()) < 200 and RESOLUTION_USER_RE.search(text):
                    found.append({
                        "session": jsonl.name,
                        "timestamp": prev_assistant_ts or obj.get("timestamp", ""),
                        "assistant": prev_assistant_text,
                        "user_confirmation": text,
                    })
                prev_assistant_text = ""

    found.sort(key=lambda r: r["timestamp"], reverse=True)
    return found


def _extract_message_text(msg: dict) -> str:
    """Same as context._extract_text but local — avoids circular import friction."""
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


# ---------- forge install-hotkey ------------------------------------------


HOTKEY_INSTRUCTIONS = """## Setting up Cmd+Option+S for skill-forge capture

This binds Cmd+Option+S system-wide to pop the capture dialog.

### One-time setup (about 2 minutes)

**1. Open Automator** (⌘+Space, type "Automator", return)

**2. File → New → "Quick Action"**

**3. Configure the Quick Action:**
   - "Workflow receives:" set to **no input**
   - "in:" set to **any application**

**4. Drag in the "Run Shell Script" action** (search the left sidebar)

**5. Paste this into the script box:**

```bash
{script_body}
```

**6. Set "Shell:" to `/bin/zsh`**

**7. File → Save** — name it `skill-forge capture`

**8. Now bind the hotkey:**
   - System Settings → Keyboard → Keyboard Shortcuts → Services
   - Find `skill-forge capture` under "General"
   - Click the area next to it and press **⌘⌥S** (Cmd+Option+S)

### Try it

Hit Cmd+Option+S anywhere. A dialog should pop asking "What did you just solve?"
Type a sentence, hit Capture, and watch the proposal appear in Obsidian.

### If something goes wrong

- If the dialog never appears, run `forge capture` in a terminal — if THAT works,
  it's a Quick Action wiring issue. Check Automator's run log.
- If the dialog appears but nothing happens after submit, check the .env path
  in the script body is correct.
"""


def cmd_install_hotkey(args, config: Config) -> int:
    """Print step-by-step instructions for binding the global hotkey via macOS Quick Actions."""
    forge_dir = Path(__file__).resolve().parent.parent
    script_body = (
        f"cd {forge_dir} && /usr/bin/env python3 -m forge capture "
        f">> ~/.skill-forge/capture.log 2>&1"
    )
    print(HOTKEY_INSTRUCTIONS.format(script_body=script_body))
    return 0


# ---------- forge init (kept, simplified) ----------------------------------


def cmd_init(args, config: Config) -> int:
    for d in [
        config.inbox_dir,
        config.proposals_dir,
        config.approved_dir,
        config.archive_dir,
        config.state_dir,
        config.skills_repo_path,
    ]:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  ✓ {d}")

    readme = config.vault_path / "README.md"
    if not readme.exists():
        readme.write_text(
            "# skill-forge vault\n\n"
            "- `inbox/` — quick notes about patterns worth capturing\n"
            "- `proposals/` — drafted SKILL.md awaiting your review\n"
            "- `approved/` — drag a proposal here to ship it\n"
            "- `archive/` — rejected or already-installed proposals\n",
            encoding="utf-8",
        )
        print(f"  ✓ {readme}")
    return 0


# ---------- forge tag ------------------------------------------------------


TAG_SYSTEM = """You are organizing a developer's Claude skills with tags so they form an interconnected graph in Obsidian.

You will receive all of their installed skills (frontmatter + first portion of body). Your job:

1. Assign 3-6 tags to EACH skill.
2. Tags must be drawn from a SHARED VOCABULARY across the whole collection. The goal is overlap — if two skills both involve NetSuite, they MUST share a `netsuite` tag, not `netsuite-erp` on one and `ns` on another. Choose one canonical form per concept.
3. Tags are kebab-case, no spaces, lowercase. Examples: `netsuite`, `integration`, `suitelet`, `internal-tooling`, `process-improvement`, `windows-setup`.
4. Tags should describe DOMAINS, TECH, and WORK MODES — not specific files or one-off concepts. Aim for tags that 3+ skills could share.

Output STRICT JSON in this exact shape, nothing else:

{
  "vocabulary": ["tag-a", "tag-b", "tag-c", ...],
  "skills": {
    "skill-name-1": ["tag-a", "tag-b", "tag-c"],
    "skill-name-2": ["tag-a", "tag-d"],
    ...
  }
}

Rules:
- `vocabulary` lists every distinct tag used across all skills.
- `skills` keys are the exact skill names (kebab-case) as given.
- Every tag in `skills[*]` must appear in `vocabulary`.
- No preamble, no markdown fences, no explanation. JSON only."""


def cmd_tag(args, config: Config) -> int:
    """Generate (and optionally apply) shared tags across all installed skills."""
    installed = list_installed_skills(config)
    if not installed:
        print("no installed skills yet")
        return 0

    # Build a compact representation: frontmatter + first ~300 chars of body.
    skill_summaries = []
    for p in installed:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = read_skill_frontmatter(p)
        name = fm.get("name", p.parent.name)
        desc = fm.get("description", "")
        # Strip frontmatter to get body snippet
        m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
        body = text[m.end():] if m else text
        body_snippet = body.strip()[:300].replace("\n", " ")
        skill_summaries.append(
            f"### {name}\n"
            f"**description:** {desc}\n"
            f"**body snippet:** {body_snippet}"
        )

    user_msg = (
        f"## {len(installed)} installed skills\n\n"
        + "\n\n---\n\n".join(skill_summaries)
        + "\n\n---\n\nProduce the JSON tag mapping."
    )

    print(f"tagging {len(installed)} skills via {config.draft_model}...")
    try:
        raw = api_call(config, TAG_SYSTEM, user_msg, max_tokens=3000)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    try:
        parsed = _parse_tag_json(raw)
    except ValueError as e:
        print(f"✗ couldn't parse model output as JSON: {e}")
        print("--- raw response ---")
        print(raw[:1000])
        return 1

    vocab = parsed.get("vocabulary", [])
    mapping = parsed.get("skills", {})

    # Validate: every tag in skills should be in vocabulary.
    unknown = set()
    for tags in mapping.values():
        for t in tags:
            if t not in vocab:
                unknown.add(t)
    if unknown:
        print(f"  ! note: model used tags not in its own vocabulary: {sorted(unknown)}")
        print("  (proceeding anyway — they'll still work in Obsidian)")

    # Show the result.
    print(f"\nvocabulary ({len(vocab)} tags): {', '.join(sorted(vocab))}")
    print(f"\nproposed assignments:")
    for p in installed:
        name = p.parent.name
        tags = mapping.get(name) or mapping.get(read_skill_frontmatter(p).get("name", ""), [])
        print(f"  {name}")
        print(f"    tags: {', '.join(tags) if tags else '(none assigned)'}")

    if not args.apply:
        print(f"\n(dry run — no files changed)")
        print(f"to apply: forge tag --apply")
        return 0

    # Apply: rewrite each SKILL.md frontmatter with the tags.
    print(f"\napplying...")
    changed_paths = []
    for p in installed:
        name = p.parent.name
        fm_name = read_skill_frontmatter(p).get("name", name)
        tags = mapping.get(name) or mapping.get(fm_name, [])
        if not tags:
            continue
        try:
            new_text = _set_tags_in_frontmatter(p.read_text(encoding="utf-8"), tags)
            p.write_text(new_text, encoding="utf-8")
            changed_paths.append(p)
            print(f"  ✓ {name}")
        except (OSError, ValueError) as e:
            print(f"  ✗ {name}: {e}")

    if not changed_paths:
        print("nothing changed")
        return 0

    # Commit and (if configured) push.
    if (config.skills_repo_path / ".git").exists():
        import subprocess
        try:
            subprocess.run(
                ["git", "-C", str(config.skills_repo_path), "add", *[str(p.relative_to(config.skills_repo_path)) for p in changed_paths]],
                check=True,
            )
            result = subprocess.run(
                ["git", "-C", str(config.skills_repo_path), "diff", "--cached", "--quiet"],
            )
            if result.returncode != 0:
                subprocess.run(
                    ["git", "-C", str(config.skills_repo_path), "commit", "-m", f"forge: tag {len(changed_paths)} skills"],
                    check=True,
                )
                print(f"  ✓ committed")
                if config.git_auto_push:
                    subprocess.run(
                        ["git", "-C", str(config.skills_repo_path), "push", config.git_remote, config.git_branch],
                        check=True,
                    )
                    print(f"  ✓ pushed to {config.git_remote}/{config.git_branch}")
        except subprocess.CalledProcessError as e:
            print(f"  ! git operation failed: {e}")

    print(f"\ndone. Open Obsidian — the tags pane should show {len(vocab)} tags now.")
    return 0


def _parse_tag_json(raw: str) -> dict:
    """Extract JSON object from possibly-fenced model output."""
    text = raw.strip()
    # Strip code fences if present.
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # If there's leading prose, try to find the first { and last }.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("no JSON object found")
        text = text[start : end + 1]
    return json.loads(text)


def _set_tags_in_frontmatter(text: str, tags: list[str]) -> str:
    """Rewrite a SKILL.md's frontmatter to include the given tags.

    - If a `tags:` line exists, replace it.
    - Otherwise, insert `tags: [...]` right after the `description:` line
      (or at the end of frontmatter if no description).
    - Body is untouched.
    """
    m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError("no frontmatter found")
    open_d, fm, close_d, body = m.group(1), m.group(2), m.group(3), m.group(4)

    tag_line = f"tags: [{', '.join(tags)}]"

    fm_lines = fm.split("\n")
    new_lines = []
    found_tags_line = False
    description_index = -1

    for i, line in enumerate(fm_lines):
        stripped = line.lstrip()
        if stripped.startswith("tags:"):
            new_lines.append(tag_line)
            found_tags_line = True
        else:
            new_lines.append(line)
            if stripped.startswith("description:"):
                description_index = len(new_lines) - 1

    if not found_tags_line:
        # Insert after description (or at end).
        if description_index >= 0:
            new_lines.insert(description_index + 1, tag_line)
        else:
            new_lines.append(tag_line)

    new_fm = "\n".join(new_lines)
    return f"{open_d}{new_fm}{close_d}{body}"


# ---------- forge status ---------------------------------------------------


def cmd_status(args, config: Config) -> int:
    proposals = list(config.proposals_dir.glob("*.md")) if config.proposals_dir.exists() else []
    approved = list(config.approved_dir.glob("*.md")) if config.approved_dir.exists() else []
    archive = list(config.archive_dir.glob("*.md")) if config.archive_dir.exists() else []
    inbox = list(config.inbox_dir.glob("*.md")) if config.inbox_dir.exists() else []
    installed = list_installed_skills(config)

    print("skill-forge status")
    print(f"  installed skills:   {len(installed)}")
    print(f"  inbox notes:        {len(inbox)}")
    print(f"  proposals/:         {len(proposals)} awaiting review")
    print(f"  approved/:          {len(approved)} pending sync")
    print(f"  archive/:           {len(archive)}")
    print(f"  skills repo:        {config.skills_repo_path}")
    print(f"  vault:              {config.vault_path}")
    return 0

COMMANDS_EOF

echo ""
echo "Verifying CLI still loads..."
python3 -m forge --help | grep capture

echo ""
echo "✓ done"
echo ""
echo "Every capture now produces a SKILL.md in proposals/, even thin notes."
echo "Thin drafts have TODO markers in the body for you to fill in."
echo ""
echo "Try it:"
echo "  python3 -m forge capture --note \"claude skill builder\""
echo "  # Should produce a thin draft this time, not refuse."
