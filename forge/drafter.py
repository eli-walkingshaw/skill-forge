"""Drafter: turn a cluster into a SKILL.md proposal."""
from __future__ import annotations
import json
import re
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from .capture import Capture
from .cluster import Cluster
from .config import Config


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


DRAFT_SYSTEM_PROMPT = """You are a skill author for Claude. Given a cluster of related task observations from a user's work, you write a SKILL.md file that captures the reusable pattern.

A SKILL.md has this structure:

---
name: short-kebab-case-name
description: One paragraph. WHAT this skill does AND WHEN it should trigger. Include specific contexts and user phrases that should activate it. Be slightly "pushy" — Claude tends to under-trigger skills. Mention concrete keywords from the domain.
---

# Title Case Name

Brief intro: one or two sentences on what this skill is for.

## When to use

Bulleted list of triggers — user phrases, file types, errors, contexts.

## The pattern

The actual reusable knowledge. Code snippets in fenced blocks. Be concrete: show the exact gotcha and the exact fix.

## Steps

1. Numbered steps Claude should follow when applying this skill.
2. Each step actionable.

## Gotchas

Anything that surprised the user when they first hit this. Past mistakes worth not repeating.

Rules:
- Keep the whole file under 200 lines.
- The description field is the most important — it controls whether the skill triggers at all.
- Don't invent details. If the captures don't show something, don't claim it.
- Use the user's actual terminology (project names, tool names) — they're domain markers.
- Output ONLY the SKILL.md content. No preamble, no code fences around the whole thing, no explanation."""


def draft_skill(config: Config, cluster: Cluster, members: list[Capture]) -> str:
    """Call the API and return raw SKILL.md text."""
    user_prompt = _build_user_prompt(cluster, members)

    body = {
        "model": config.draft_model,
        "max_tokens": 4000,
        "system": DRAFT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error {e.code}: {err_body}") from e

    parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    text = "\n".join(parts).strip()
    return _clean_output(text)


def _build_user_prompt(cluster: Cluster, members: list[Capture]) -> str:
    lines = [
        f"Cluster ID: {cluster.id}",
        f"Number of related observations: {len(members)}",
        f"Top recurring terms: {', '.join(cluster.top_terms) or '(none)'}",
        f"Representative goal: {cluster.representative_goal}",
        "",
        "## Observations",
        "",
    ]
    for i, c in enumerate(members, 1):
        lines.append(f"### Observation {i} ({c.source}, {c.timestamp})")
        lines.append(f"**Goal:** {c.goal}")
        if c.tools:
            lines.append(f"**Tools:** {', '.join(c.tools)}")
        lines.append(f"**Pattern observed:**\n{c.pattern}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Write a SKILL.md that captures the recurring pattern across these observations. "
        "Focus on what's reusable — not the one-off details of any single observation."
    )
    return "\n".join(lines)


FENCE_WRAP_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _clean_output(text: str) -> str:
    """Strip a code fence the model sometimes wraps the whole file in."""
    m = FENCE_WRAP_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


def proposal_path(config: Config, cluster: Cluster, skill_name: str) -> Path:
    """Where to write the proposal. Uses a date prefix for Obsidian sort order."""
    date = datetime.utcnow().strftime("%Y-%m-%d")
    safe = re.sub(r"[^a-z0-9-]+", "-", skill_name.lower()).strip("-") or cluster.id
    return config.proposals_dir / f"{date}__{safe}__{cluster.id}.md"


SKILL_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def extract_skill_name(skill_md: str) -> str:
    m = SKILL_NAME_RE.search(skill_md)
    return m.group(1) if m else "unnamed-skill"


def wrap_proposal(skill_md: str, cluster: Cluster, member_count: int) -> str:
    """Wrap the SKILL.md in an Obsidian-friendly proposal note with review metadata."""
    header = f"""> [!info] skill-forge proposal
> **cluster:** `{cluster.id}` ({member_count} observations)
> **top terms:** {', '.join(cluster.top_terms) or '_(none)_'}
>
> Review this draft below. If it looks good, **drag this file to `approved/`** — the watcher will commit and push it. If it doesn't, drag it to `archive/`.
>
> The SKILL.md content starts at the next `---` divider. Everything below that divider is what gets written to the skills repo.

---

"""
    return header + skill_md
