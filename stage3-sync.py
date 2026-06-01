#!/usr/bin/env python3
"""Stage 3: forge sync — scan subscriptions for new skills, drop into pending/.

For each subscription:
  - Walk the clone for SKILL.md files (per subscription's filter pattern)
  - Compute content hash; skip if already seen (recorded per-subscription)
  - Call Claude once per new skill: suggest a stack + one-line summary
  - Write a proposal to vault/pending/<name>.md with provenance + original SKILL.md
  - Record hash so reruns don't re-propose

Stage 4 (separate patch) wires pending/ to the watcher so dragging from
pending/ to approved/ copies the whole skill directory.

Adds:
  - forge/sync.py — scan + claude-call + proposal writer
  - `forge sync` command (with --dry-run, optional <subscription-name>)
  - State tracking: subscriptions.json gets a `seen_hashes` list per subscription

Run from inside ~/code/skill-forge:
    python3 stage3-sync.py
"""
import ast
import re
import sys
from pathlib import Path


SYNC_PY = '''"""Sync subscribed repos: scan for new SKILL.md files, propose them.

The flow:
  walk(subscribed/<name>/) -> find SKILL.md -> read & hash
    -> skip if hash seen before
    -> call Claude for stack + summary
    -> write to vault/pending/<sanitized-name>.md
    -> record hash in subscriptions.json
"""
from __future__ import annotations
import fnmatch
import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .subscriptions import (
    SUBSCRIPTIONS_FILE,
    _load_state,
    _save_state,
    list_subscriptions,
    get_subscription,
)


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"


SUGGESTION_SYSTEM = """You are helping triage an external NetSuite/agent skill that's being imported into a team's skill library.

The team has three stacks: `engineering`, `data`, `operations`. A skill can be in more than one. Some skills shouldn't be published to any stack (overly internal, references named people, etc) — for those, say `never`.

Given the SKILL.md below, respond with STRICT JSON only — no preamble, no fences:

{
  "stacks": ["engineering"] or ["data", "engineering"] or "never",
  "summary": "one-line summary of what this skill does (max 100 chars)",
  "notes": "anything worth flagging on import — naming collisions, sensitive content, unusual structure, etc. Empty string if nothing."
}

Heuristics:
- Engineering: SuiteScript, Suitelets, deployment, code patterns, debugging, architecture
- Data: queries, reports, SuiteQL, search APIs, data extraction, reconciliation
- Operations: workflows, processes, business automation, role/permission setup, documentation generation
- A skill referencing source code is usually engineering. A skill that's a reference/lookup (like records-reference) is engineering + data.
- A skill about security practices is engineering."""


@dataclass
class SyncResult:
    """Per-subscription summary."""
    subscription_name: str
    found: int = 0
    new: int = 0
    skipped_seen: int = 0
    errors: list[str] = None
    proposals: list[Path] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.proposals is None:
            self.proposals = []


def sync_subscription(
    config: Config,
    subscription_name: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> SyncResult:
    """Walk one subscription's clone, propose new skills into vault/pending/."""
    sub = get_subscription(subscription_name)
    if not sub:
        return SyncResult(
            subscription_name=subscription_name,
            errors=[f"no subscription named '{subscription_name}'"],
        )
    if not sub.clone_path.exists():
        return SyncResult(
            subscription_name=subscription_name,
            errors=[f"clone missing at {sub.clone_path}"],
        )

    result = SyncResult(subscription_name=subscription_name)

    # Walk the clone for SKILL.md files matching the filter
    candidates = _find_skill_files(sub.clone_path, sub.filter)
    result.found = len(candidates)

    if not candidates:
        return result

    # Pull seen-hash list for this subscription
    seen_hashes = _get_seen_hashes(subscription_name)

    pending_dir = config.vault_path / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    new_hashes = []
    for skill_md_path in candidates:
        try:
            content = skill_md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            result.errors.append(f"{skill_md_path.name}: {e}")
            continue
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            result.skipped_seen += 1
            continue

        # Extract the skill's directory (skill = the folder containing SKILL.md)
        skill_dir = skill_md_path.parent
        skill_name = skill_dir.name

        # Get Claude's stack suggestion (skip in dry-run)
        if dry_run:
            suggestion = {"stacks": ["(dry-run)"], "summary": "(dry-run)", "notes": ""}
        else:
            try:
                suggestion = _ask_claude_for_stack(content, api_key=api_key, model=model)
            except RuntimeError as e:
                result.errors.append(f"{skill_name}: claude call failed: {e}")
                continue

        # Build the proposal file
        proposal_path = pending_dir / f"{skill_name}.md"
        # Collision handling: if file exists, suffix with -2, -3
        n = 2
        while proposal_path.exists():
            proposal_path = pending_dir / f"{skill_name}-{n}.md"
            n += 1

        rel_path = skill_md_path.relative_to(sub.clone_path)
        proposal_text = _build_proposal(
            skill_name=skill_name,
            skill_md_content=content,
            subscription_name=sub.name,
            source_url=sub.url,
            source_sha=sub.last_pulled_sha,
            source_path=str(rel_path),
            suggestion=suggestion,
            skill_dir=skill_dir,
        )

        if not dry_run:
            proposal_path.write_text(proposal_text, encoding="utf-8")
            new_hashes.append(content_hash)

        result.new += 1
        result.proposals.append(proposal_path)

    # Update seen hashes if we made progress
    if not dry_run and new_hashes:
        _add_seen_hashes(subscription_name, new_hashes)

    return result


def sync_all(
    config: Config,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> list[SyncResult]:
    """Sync every subscription. Returns one result per subscription."""
    results = []
    for sub in list_subscriptions():
        results.append(
            sync_subscription(
                config, sub.name, api_key=api_key, model=model, dry_run=dry_run
            )
        )
    return results


def _find_skill_files(clone_root: Path, filter_glob: str) -> list[Path]:
    """Walk the clone and return SKILL.md files matching the filter pattern."""
    matches = []
    for p in clone_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(clone_root)
        except ValueError:
            continue
        rel_str = str(rel)
        # Match the filter pattern (fnmatch can't do **/, so handle that case)
        if fnmatch.fnmatch(rel_str, filter_glob):
            matches.append(p)
        elif filter_glob.startswith("**/"):
            # ** prefix → match anywhere in the tree
            tail = filter_glob[3:]
            if fnmatch.fnmatch(p.name, tail) or fnmatch.fnmatch(rel_str, "*/" + tail):
                matches.append(p)
    # Dedup (a single file might match more than one path-form)
    return sorted(set(matches))


def _ask_claude_for_stack(
    skill_md: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 600,
) -> dict:
    """One Claude call: returns {"stacks": [...], "summary": "...", "notes": "..."}."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": SUGGESTION_SYSTEM,
        "messages": [{"role": "user", "content": f"```\\n{skill_md}\\n```"}],
    }
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API error {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}")

    text_parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\\n".join(text_parts).strip()
    return _parse_json_object(text)


def _parse_json_object(text: str) -> dict:
    """Extract a JSON object from possibly-fenced model output."""
    t = text.strip()
    m = re.match(r"^```(?:json)?\\s*\\n(.*)\\n```\\s*$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        start = t.find("{")
        end = t.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise RuntimeError(f"no JSON object found in model output: {text[:200]}")
        t = t[start : end + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"model output was not valid JSON: {e}")


def _build_proposal(
    *,
    skill_name: str,
    skill_md_content: str,
    subscription_name: str,
    source_url: str,
    source_sha: str,
    source_path: str,
    suggestion: dict,
    skill_dir: Path,
) -> str:
    """Build a vault/pending/<name>.md file content.

    Format mirrors what cmd_capture produces — Obsidian callout + divider +
    actual SKILL.md content. Includes provenance and Claude's stack suggestion.
    """
    suggested_stacks = suggestion.get("stacks", [])
    if isinstance(suggested_stacks, str):
        # Could be "never"
        suggested_label = suggested_stacks
    else:
        suggested_label = ", ".join(suggested_stacks) if suggested_stacks else "(none)"

    summary = suggestion.get("summary") or "(no summary)"
    notes = suggestion.get("notes") or ""

    # Count companion files (references/, scripts/, assets/) so user knows
    # what gets copied on approval
    companion_count = 0
    for child in skill_dir.iterdir():
        if child.name == "SKILL.md":
            continue
        if child.is_file():
            companion_count += 1
        elif child.is_dir():
            companion_count += sum(1 for _ in child.rglob("*") if _.is_file())

    callout_lines = [
        f"> [!info] skill-forge proposal (imported)",
        f"> **skill:** `{skill_name}`",
        f"> **source:** {subscription_name} @ {source_sha[:8]}",
        f"> **path:** {source_path}",
        f"> **url:** {source_url}",
        f"> **summary:** {summary}",
        f"> **suggested stacks:** {suggested_label}",
        f"> **companion files:** {companion_count} (will copy on approval)",
    ]
    if notes:
        callout_lines.append(f"> **notes:** {notes}")
    callout_lines.extend([
        f">",
        f"> When ready: drag to approved/. Reject: drag to archive/.",
        f">",
        f"> The original SKILL.md is below the divider. The frontmatter has been",
        f"> annotated with `stacks:` and provenance.",
        f"",
        f"---",
        f"",
    ])

    # Annotate the original SKILL.md frontmatter with stacks + provenance
    annotated = _annotate_frontmatter(
        skill_md_content,
        stacks=suggested_stacks if isinstance(suggested_stacks, list) else [],
        never_publish=(suggested_stacks == "never"),
        source_url=source_url,
        source_sha=source_sha,
        source_path=source_path,
    )

    return "\\n".join(callout_lines) + annotated


def _annotate_frontmatter(
    skill_md: str,
    *,
    stacks: list[str],
    never_publish: bool,
    source_url: str,
    source_sha: str,
    source_path: str,
) -> str:
    """Insert stacks: and forge_source: into the SKILL.md frontmatter.

    Preserves existing frontmatter fields; only adds/updates ours.
    """
    m = re.match(r"^(---\\s*\\n)(.*?)(\\n---\\s*\\n)(.*)$", skill_md, re.DOTALL)
    if not m:
        # No frontmatter — bail and return original. The validator will flag it.
        return skill_md

    fm_open, fm_body, fm_close, body = m.group(1), m.group(2), m.group(3), m.group(4)

    # Remove any pre-existing stacks: / never_publish: / forge_source: lines
    cleaned = []
    for line in fm_body.splitlines():
        ls = line.lstrip()
        if ls.startswith("stacks:") or ls.startswith("never_publish:") or ls.startswith("forge_source:"):
            continue
        cleaned.append(line)

    # Append our annotations
    if never_publish:
        cleaned.append("never_publish: true")
    elif stacks:
        cleaned.append(f"stacks: [{', '.join(stacks)}]")
    cleaned.append(f"forge_source: {source_url}@{source_sha[:8]}:{source_path}")

    return fm_open + "\\n".join(cleaned) + fm_close + body


# ---- seen-hash tracking, persisted in subscriptions.json ----

def _get_seen_hashes(subscription_name: str) -> set[str]:
    state = _load_state()
    for entry in state.get("subscriptions", []):
        if entry.get("name") == subscription_name:
            return set(entry.get("seen_hashes", []))
    return set()


def _add_seen_hashes(subscription_name: str, new_hashes: list[str]) -> None:
    state = _load_state()
    for entry in state.get("subscriptions", []):
        if entry.get("name") == subscription_name:
            existing = entry.get("seen_hashes", [])
            entry["seen_hashes"] = sorted(set(existing) | set(new_hashes))
            break
    _save_state(state)
'''


COMMANDS_PY_ADDITIONS = '''

# ---------- forge sync -----------------------------------------------------


def cmd_sync(args, config: Config) -> int:
    from .sync import sync_subscription, sync_all

    if not config.anthropic_api_key:
        print("X ANTHROPIC_API_KEY not set — can't call claude for stack suggestions")
        return 1

    if args.name:
        results = [
            sync_subscription(
                config,
                args.name,
                api_key=config.anthropic_api_key,
                dry_run=args.dry_run,
            )
        ]
    else:
        results = sync_all(
            config,
            api_key=config.anthropic_api_key,
            dry_run=args.dry_run,
        )

    if not results:
        print("(no subscriptions to sync — use `forge subscribe add` first)")
        return 0

    total_new = 0
    total_errors = 0
    for r in results:
        marker = "*" if r.new else " "
        print(f"  {marker} {r.subscription_name}: {r.new} new, {r.skipped_seen} already-seen, {r.found} total found")
        total_new += r.new
        total_errors += len(r.errors)
        for err in r.errors:
            print(f"      error: {err}")
        for p in r.proposals:
            print(f"      + {p.name}")

    print()
    if args.dry_run:
        print("(dry run — no proposals were written, no API calls were made)")
    else:
        print(f"summary: {total_new} new proposal(s) in vault/pending/")
        if total_errors:
            print(f"         {total_errors} error(s)")
        if total_new:
            print()
            print("Review in Obsidian and drag from pending/ to approved/ to install.")
    return 0
'''


MAIN_PY_ADDITIONS = """
    sync_p = sub.add_parser("sync", help="Scan subscribed repos, propose new skills into pending/")
    sync_p.add_argument("name", nargs="?", help="Specific subscription to sync (omit for all)")
    sync_p.add_argument("--dry-run", action="store_true", help="Show what would happen, no writes, no API calls")
    sync_p.set_defaults(fn=cmd_sync)
"""


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "subscriptions.py").exists():
        print("X forge/subscriptions.py not found — run stage2-subscribe.py first")
        return 1

    # Step 1: write forge/sync.py
    sync_path = forge_dir / "sync.py"
    if sync_path.exists() and "def sync_subscription" in sync_path.read_text():
        print("  + forge/sync.py already exists (overwriting with latest)")
    sync_path.write_text(SYNC_PY)
    print("  + wrote forge/sync.py")

    # Step 2: add cmd_sync to commands.py
    commands_path = forge_dir / "commands.py"
    cmds = commands_path.read_text()
    if "def cmd_sync(" in cmds:
        print("  + cmd_sync already in commands.py (skipping)")
    else:
        cmds = cmds.rstrip() + "\n" + COMMANDS_PY_ADDITIONS
        commands_path.write_text(cmds)
        print("  + added cmd_sync to commands.py")

    # Step 3: wire cmd_sync into __main__.py
    main_path = forge_dir / "__main__.py"
    main_src = main_path.read_text()

    if "cmd_sync" not in main_src:
        import_re = re.compile(r"(from \.commands import \(\n)(.*?)(\n\))", re.DOTALL)
        m = import_re.search(main_src)
        if m:
            body = m.group(2)
            if not body.rstrip().endswith(","):
                body = body.rstrip() + ","
            new_body = body + "\n    cmd_sync,"
            new_import = m.group(1) + new_body + m.group(3)
            main_src = main_src[:m.start()] + new_import + main_src[m.end():]
            print("  + added cmd_sync to __main__.py imports")

        marker = "    return p"
        if marker in main_src and 'sync_p = sub.add_parser("sync"' not in main_src:
            main_src = main_src.replace(marker, MAIN_PY_ADDITIONS + "\n" + marker, 1)
            print("  + added sync subparser to build_parser")
        main_path.write_text(main_src)
    else:
        print("  + __main__.py already has cmd_sync wiring (skipping)")

    # Clear .pyc
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    # Parse-check
    try:
        ast.parse(sync_path.read_text())
        ast.parse(commands_path.read_text())
        ast.parse(main_path.read_text())
        print("  + all files parse cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    print()
    print("+ stage 3 complete")
    print()
    print("Try:")
    print("  python3 -m forge sync --dry-run                       # safe preview (no API calls)")
    print("  python3 -m forge sync netsuite-suitecloud             # actually sync one sub")
    print("  python3 -m forge sync                                 # sync all subs")
    print()
    print("Cost note: each new skill triggers one Claude API call (~$0.01 each).")
    print("For your netsuite-suitecloud subscription, expect 7 calls on first sync.")
    print()
    print("Proposals land in vault/pending/ — drag to approved/ to install (stage 4 wires that).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
