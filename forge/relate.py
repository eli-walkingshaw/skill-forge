"""Use Claude to assign `related:` links between skills.

For every SKILL.md in the canonical repo, call Claude once with the skill's
content + a catalog of all other skills (names + descriptions). Claude picks
which ones are genuinely related and we write them back as `related:` Obsidian
wikilinks in the frontmatter.

The wikilink syntax lets Obsidian's graph view follow them.
"""
from __future__ import annotations
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .stacks import _is_blessed_stack


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"


RELATE_SYSTEM = """You are helping build a knowledge graph of Anthropic-style skills.

You will be given:
1. One target SKILL.md (the skill we're picking related skills FOR)
2. A catalog of every OTHER skill in the library, with name + description

Pick the skills genuinely related to the target. "Related" means a coder working with the target skill would benefit from also knowing the related skill — they share a problem domain, complement each other, or one is a prerequisite for the other. Strong, content-grounded relationships only.

Respond with STRICT JSON only — no preamble, no fences:

{
  "related": [
    {"name": "exact-skill-name-from-catalog", "why": "brief reason (under 80 chars)"},
    ...
  ]
}

Rules:
- Use EXACT skill names from the catalog. Names are kebab-case.
- No cap on how many related skills, but quality > quantity. Pick fewer if only 2-3 are genuinely related.
- Skip vague or weak connections ("both involve NetSuite" isn't enough).
- Don't include the target skill itself in its own `related` list.
- If genuinely nothing is related, return {"related": []}.
"""


@dataclass
class SkillInfo:
    """One skill catalog entry."""
    name: str
    description: str
    skill_md_path: Path
    stack: str


@dataclass
class RelateResult:
    skill_name: str
    related: list[dict]  # [{"name": ..., "why": ...}]
    error: str = ""


def walk_canonical_skills(config: Config) -> list[SkillInfo]:
    """Walk torus-skills/<stack>/<name>/SKILL.md, skipping symlinks (secondary)
    and never_publish skills.
    """
    out = []
    repo = config.skills_repo_path
    if not repo.exists():
        return out

    for stack_dir in sorted(repo.iterdir()):
        if not stack_dir.is_dir() or stack_dir.name.startswith("."):
            continue
        if not _is_blessed_stack(stack_dir.name):
            continue
        for skill_dir in sorted(stack_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if skill_dir.is_symlink():
                continue  # skip secondary-stack symlinks; canonical is in another stack
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            if fm.get("never_publish"):
                continue
            desc = (fm.get("description") or "").strip()
            out.append(SkillInfo(
                name=skill_dir.name,
                description=desc,
                skill_md_path=skill_md,
                stack=stack_dir.name,
            ))
    return out


def relate_one(
    target: SkillInfo,
    catalog: list[SkillInfo],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> RelateResult:
    """Call Claude once to determine which catalog entries relate to target."""
    others = [s for s in catalog if s.name != target.name]
    if not others:
        return RelateResult(skill_name=target.name, related=[])

    try:
        target_text = target.skill_md_path.read_text(encoding="utf-8")
    except OSError as e:
        return RelateResult(skill_name=target.name, related=[], error=f"read failed: {e}")

    catalog_lines = []
    for o in others:
        # Keep description compact; one line per skill in the catalog
        d = (o.description or "(no description)")[:200]
        catalog_lines.append(f"- {o.name}: {d}")
    catalog_str = "\n".join(catalog_lines)

    user_msg = (
        f"TARGET SKILL ({target.name}):\n\n```\n{target_text}\n```\n\n"
        f"CATALOG OF OTHER SKILLS:\n{catalog_str}\n\n"
        f"Pick the skills genuinely related to the target. JSON only."
    )

    try:
        resp = _call_claude(user_msg, api_key=api_key, model=model)
    except RuntimeError as e:
        return RelateResult(skill_name=target.name, related=[], error=str(e))

    try:
        parsed = _parse_json_object(resp)
    except RuntimeError as e:
        return RelateResult(skill_name=target.name, related=[], error=f"parse: {e}")

    related = parsed.get("related", [])
    if not isinstance(related, list):
        return RelateResult(skill_name=target.name, related=[], error="model didn't return a list")

    # Filter to only names that actually exist in the catalog
    catalog_names = {s.name for s in others}
    clean = []
    for item in related:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "").strip()
        why = item.get("why", "").strip()
        if name in catalog_names:
            clean.append({"name": name, "why": why})

    return RelateResult(skill_name=target.name, related=clean)


def write_related(target: SkillInfo, related: list[dict]) -> bool:
    """Write `related: [[skill-a]], [[skill-b]]...` into the SKILL.md frontmatter.

    The wikilink syntax is what Obsidian's graph view consumes. We write them
    as a YAML inline list of quoted strings containing the wikilink syntax.

    Returns True if file changed.
    """
    try:
        text = target.skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return False

    fm_match = re.match(r"^(---[ \t]*\n)(.*?)(\n---[ \t]*\n)(.*)$", text, re.DOTALL)
    if not fm_match:
        # No frontmatter — bail (we don't add one here; the skill is malformed)
        return False

    fm_open, fm_body, fm_close, body = fm_match.group(1), fm_match.group(2), fm_match.group(3), fm_match.group(4)

    # Build the new related: line. Empty list if nothing related.
    if related:
        # Quote each wikilink string for safe YAML
        items = [f'"[[{r["name"]}]]"' for r in related]
        related_line = f"related: [{', '.join(items)}]"
    else:
        related_line = "related: []"

    # Remove any existing `related:` line from frontmatter body
    new_lines = []
    for line in fm_body.splitlines():
        if line.lstrip().startswith("related:"):
            continue
        new_lines.append(line)
    # Append the new related line
    new_lines.append(related_line)
    new_body = "\n".join(new_lines)

    new_text = fm_open + new_body + fm_close + body
    if new_text == text:
        return False
    try:
        target.skill_md_path.write_text(new_text, encoding="utf-8")
        return True
    except OSError:
        return False


def relate_all(
    config: Config,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    only_skill: str = "",
    sleep_between: float = 8.0,
) -> list[RelateResult]:
    """Relate every skill in the catalog. Optional `only_skill` to relate just one."""
    catalog = walk_canonical_skills(config)
    if not catalog:
        return []

    if only_skill:
        targets = [s for s in catalog if s.name == only_skill]
        if not targets:
            return [RelateResult(skill_name=only_skill, related=[], error="not found")]
    else:
        targets = catalog

    results = []
    for i, target in enumerate(targets):
        if dry_run:
            results.append(RelateResult(skill_name=target.name, related=[]))
            continue

        result = relate_one(target, catalog, api_key=api_key, model=model)
        if not result.error:
            write_related(target, result.related)
        results.append(result)

        # Sleep between calls to dodge rate limits, but not after the last one
        if i < len(targets) - 1 and sleep_between > 0:
            time.sleep(sleep_between)

    return results


# ---- helpers ----

_FM_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.lower() in ("true", "false"):
            out[key] = (val.lower() == "true")
        else:
            out[key] = val
    return out


def _call_claude(user_msg: str, *, api_key: str, model: str = DEFAULT_MODEL,
                 max_tokens: int = 1024) -> str:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": RELATE_SYSTEM,
        "messages": [{"role": "user", "content": user_msg}],
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

    parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(parts).strip()


def _parse_json_object(text: str) -> dict:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        start = t.find("{")
        end = t.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise RuntimeError(f"no JSON object found")
        t = t[start : end + 1]
    return json.loads(t)
