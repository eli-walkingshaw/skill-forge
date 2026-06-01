#!/usr/bin/env bash
# Upgrade an existing skill-forge install to the new workflow.
# Run from inside ~/code/skill-forge.
set -euo pipefail

[[ -f forge/__main__.py ]] || { echo "Run from ~/code/skill-forge"; exit 1; }

echo "Backing up old files..."
mkdir -p .upgrade-backup
cp forge/__main__.py .upgrade-backup/ 2>/dev/null || true
cp README.md .upgrade-backup/ 2>/dev/null || true
cp docs/SETUP.md .upgrade-backup/ 2>/dev/null || true
echo "  ✓ originals saved to .upgrade-backup/"

echo ""
echo "Writing new forge/commands.py..."
cat > forge/commands.py << 'COMMANDS_EOF'
"""Commands that build/edit skills, using existing skills as style reference."""
from __future__ import annotations
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime
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

echo "Writing new forge/__main__.py..."
cat > forge/__main__.py << 'MAIN_EOF'
"""skill-forge CLI."""
from __future__ import annotations
import argparse
import sys

from .config import load_config
from .commands import (
    cmd_init,
    cmd_new,
    cmd_draft,
    cmd_edit,
    cmd_inbox_to_skill,
    cmd_audit,
    cmd_list,
    cmd_status,
)
from .watch import watch_loop


def cmd_watch(args, config) -> int:
    watch_loop(config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge", description="Claude skill workshop")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create vault folders (idempotent)").set_defaults(fn=cmd_init)

    new_p = sub.add_parser("new", help="Scaffold a blank SKILL.md in proposals/")
    new_p.add_argument("name", help="Skill name (kebab-case or any string)")
    new_p.add_argument("--force", action="store_true", help="Overwrite existing proposal of same name")
    new_p.set_defaults(fn=cmd_new)

    draft_p = sub.add_parser("draft", help="Claude drafts a SKILL.md from a one-line description")
    draft_p.add_argument("description", nargs="+", help="What the skill should do")
    draft_p.set_defaults(fn=cmd_draft)

    edit_p = sub.add_parser("edit", help="Pull an installed skill into proposals/ for editing")
    edit_p.add_argument("name", help="Installed skill name")
    edit_p.set_defaults(fn=cmd_edit)

    its_p = sub.add_parser("inbox-to-skill", help="Promote an inbox note into a drafted SKILL.md")
    its_p.add_argument("note", help="Path to the note (or filename if it's in inbox/)")
    its_p.set_defaults(fn=cmd_inbox_to_skill)

    sub.add_parser("audit", help="Claude reviews your skills for gaps/staleness/dupes").set_defaults(fn=cmd_audit)
    sub.add_parser("list", help="List installed skills").set_defaults(fn=cmd_list)
    sub.add_parser("status", help="Show pipeline state").set_defaults(fn=cmd_status)
    sub.add_parser("watch", help="Daemon: validate + commit approved/ → repo").set_defaults(fn=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except RuntimeError as e:
        print(f"config error: {e}", file=sys.stderr)
        print("Tip: copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2
    return args.fn(args, config)


if __name__ == "__main__":
    sys.exit(main())

MAIN_EOF

echo "Writing new README.md..."
cat > README.md << 'README_EOF'
# skill-forge

A workshop for Claude skills. Drafts, edits, and audits SKILL.md files; reviews live in Obsidian; approved skills auto-install into your skills repo and sync to Git.

## Commands

| Command | What it does |
|---|---|
| `forge new <name>` | Drop a blank SKILL.md template into `proposals/` for you to fill in |
| `forge draft "<description>"` | Claude drafts a SKILL.md from a one-line description, using your existing skills as style reference |
| `forge edit <name>` | Pull an installed skill into `proposals/` so you can edit it safely |
| `forge inbox-to-skill <note>` | Promote an inbox note into a drafted SKILL.md |
| `forge audit` | Claude reviews your skills for gaps, staleness, duplicates, weak descriptions |
| `forge list` | Show all installed skills |
| `forge status` | Pipeline state (proposals awaiting review, etc.) |
| `forge watch` | Daemon: when you drag a proposal to `approved/`, validate + install + commit |
| `forge init` | Create vault folders (idempotent) |

## The loop

```
forge new / draft / edit / inbox-to-skill
        ↓
   vault/proposals/  ← review in Obsidian
        ↓
   drag → vault/approved/
        ↓
   forge watch (daemon)
        ↓
   skills-repo/<name>/SKILL.md  ← committed
        ↓
   symlinked into ~/.claude/skills/user
        ↓
   Claude Code sees it
```

## Quick start

```bash
cp .env.example .env
# Edit .env — at minimum: ANTHROPIC_API_KEY, VAULT_PATH, SKILLS_REPO_PATH
python3 -m forge init
python3 -m forge list           # see what's already installed
python3 -m forge draft "handle the NetSuite SuiteQL column-name traps"
# review the proposal in Obsidian, then drag it to approved/
python3 -m forge watch &        # background daemon — installs anything approved
```

## Why this shape

Most skill-authoring is iterative: you write a draft, refine the description, test the trigger, edit again. Pushing that loop through Obsidian (instead of editing files in the skills repo directly) gives you:

- A safe review surface — the draft isn't live until you move it
- Git hygiene — every approved skill is a clean commit
- A natural inbox for half-formed pattern observations that may or may not graduate to skills

The earlier version of this tool tried to auto-detect patterns from Claude Code session logs and cluster them into skill candidates. That worked technically but produced low-signal results, because session logs are mostly help-seeking ("X is broken, help") not pattern-stating ("I solved X by doing Y"). The current version drops auto-detection and assumes you know when a skill is needed — you just want low-friction tools to write it.

README_EOF

echo ""
echo "Cleaning up stale state from the old auto-detect workflow..."
rm -f ~/.skill-forge/captures.jsonl ~/.skill-forge/clusters.json ~/.skill-forge/drafted.json
echo "  ✓ removed old captures/clusters state"

echo ""
echo "Testing the new CLI..."
python3 -m forge --help

echo ""
echo "Verifying it sees your installed skills..."
python3 -m forge list | head -20

echo ""
echo "✓ upgrade complete"
echo ""
echo "Try:"
echo "  python3 -m forge list"
echo "  python3 -m forge status"
echo "  python3 -m forge draft \"handle SuiteQL column-name traps\""
echo "  python3 -m forge audit    # Claude reviews your 14 skills"
