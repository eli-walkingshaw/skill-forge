"""Sync subscribed repos: scan for new SKILL.md files, propose them.

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
        "messages": [{"role": "user", "content": f"```\n{skill_md}\n```"}],
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
    text = "\n".join(text_parts).strip()
    return _parse_json_object(text)


def _parse_json_object(text: str) -> dict:
    """Extract a JSON object from possibly-fenced model output."""
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", t, re.DOTALL)
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
        f"---",
        f"tags: [pending]",
        f"---",
        f"",
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

    return "\n".join(callout_lines) + annotated


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
    m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)(.*)$", skill_md, re.DOTALL)
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

    return fm_open + "\n".join(cleaned) + fm_close + body


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
