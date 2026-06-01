"""Stack discovery, diff, and publish.

A stack is a curated subset of your skills, derived from `stacks:` frontmatter
entries on individual SKILL.md files. Each stack publishes to its own
read-only-by-others git repo so teammates can clone just their slice.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LIST_RE = re.compile(r"\[(.*?)\]")


# ---- BLESSED_STACKS filter ------------------------------------------------
# Skill repos contain stack folders (data/, engineering/, operations/, etc.)
# AND workflow folders (pending/, archive/, etc.) that LOOK like stacks but
# are not. Anything not in this set is excluded from skill discovery.
BLESSED_STACKS = {"data", "engineering", "operations", "unassigned"}


def _is_blessed_stack(dir_name: str) -> bool:
    """Return True if dir_name is a real stack (not pending/, archive/, etc).

    Used to filter repo.iterdir() calls so non-stack folders don't leak into
    skill walks. Dotfiles (.git, .github) are also excluded.
    """
    if dir_name.startswith("."):
        return False
    return dir_name in BLESSED_STACKS


def _parse_frontmatter(skill_path: Path) -> dict:
    """Best-effort YAML parser. Handles flat key: value and simple `[a, b]` lists."""
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # List? e.g. tags: [a, b, c]
        list_m = _LIST_RE.match(value)
        if list_m:
            inner = list_m.group(1)
            items = [t.strip().strip(chr(34) + chr(39) + chr(96) + " ") for t in inner.split(",")]
            out[key] = [i for i in items if i]
        elif value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


@dataclass
class Stack:
    name: str
    skills: list[Path] = field(default_factory=list)
    repo_url: str = ""             # from STACK_REPO_<name> env var; "" = local only

    @property
    def local_repo_path(self) -> Path:
        return Path.home() / "code" / f"torus-skills-{self.name}"


def discover_stacks(config: Config) -> dict[str, Stack]:
    """Walk the canonical skills repo (nested layout) and group skills by stack.

    Expected layout: <repo>/<stack>/<skill-name>/SKILL.md
    Skills with `never_publish: true` are excluded.
    """
    repo = config.skills_repo_path
    if not repo.exists():
        return {}

    stacks: dict[str, Stack] = {}
    for stack_dir in sorted(repo.iterdir()):
        if not stack_dir.is_dir():
            continue
        if not _is_blessed_stack(stack_dir.name):
            continue
        # 'unassigned' is blessed but skipped from discover_stacks (it's not a real stack, just a holding bin).
        if stack_dir.name == "unassigned":
            continue
        for skill_dir in sorted(stack_dir.iterdir()):
            # follow symlinks here — they are valid skill homes for this stack
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            fm = _parse_frontmatter(skill_md)
            if fm.get("never_publish"):
                continue
            sname = stack_dir.name
            if sname not in stacks:
                stacks[sname] = Stack(name=sname)
            stacks[sname].skills.append(skill_md)

    # Pull repo URLs from environment.
    import os
    for name, stack in stacks.items():
        env_key = f"STACK_REPO_{name}"
        stack.repo_url = os.environ.get(env_key, "").strip()

    return stacks


def list_skill_assignments(config: Config) -> dict[Path, list[str]]:
    """Return {skill_path: [stack names from frontmatter]} for every skill found.

    Walks the nested layout. Reports the canonical SKILL.md path for each skill
    (not its symlinked alias paths).
    """
    repo = config.skills_repo_path
    out: dict[Path, list[str]] = {}
    if not repo.exists():
        return out
    seen_canonical: set[Path] = set()
    for stack_dir in sorted(repo.iterdir()):
        if not stack_dir.is_dir() or stack_dir.name.startswith("."):
            continue
        if not _is_blessed_stack(stack_dir.name):
            continue
        for skill_dir in sorted(stack_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            # Resolve symlinks so we only report each skill once at its canonical path
            try:
                resolved = skill_dir.resolve()
            except OSError:
                continue
            if resolved in seen_canonical:
                continue
            seen_canonical.add(resolved)
            skill_md = resolved / "SKILL.md"
            if not skill_md.exists():
                continue
            fm = _parse_frontmatter(skill_md)
            if fm.get("never_publish"):
                out[skill_md] = ["(never_publish)"]
                continue
            ss = fm.get("stacks") or []
            if isinstance(ss, str):
                ss = [ss]
            out[skill_md] = ss
    return out


def diff_stack(config: Config, stack: Stack) -> dict:
    """What would change if we published this stack now.

    Returns: {"added": [...], "updated": [...], "removed": [...]}
    where paths are relative to the stack repo root.
    """
    target_dir = stack.local_repo_path
    diff = {"added": [], "updated": [], "removed": []}

    # Build the set of skills that should be in the published stack
    desired = {s.parent.name: s for s in stack.skills}

    # What's currently in the target stack repo?
    current = set()
    if target_dir.exists():
        for child in target_dir.iterdir():
            if child.is_dir() and (child / "SKILL.md").exists():
                current.add(child.name)

    for name, source_skill in desired.items():
        if name not in current:
            diff["added"].append(name)
        else:
            existing = target_dir / name / "SKILL.md"
            try:
                if existing.read_text() != source_skill.read_text():
                    diff["updated"].append(name)
            except OSError:
                diff["updated"].append(name)

    for name in current:
        if name not in desired:
            diff["removed"].append(name)

    return diff


def publish_stack(config: Config, stack: Stack, *, dry_run: bool = False) -> dict:
    """Generate the stack repo and (optionally) push.

    Returns the diff that was applied (so the caller can decide to notify).
    """
    target_dir = stack.local_repo_path
    diff = diff_stack(config, stack)

    if dry_run:
        return diff

    # Initialize/clone the target repo if needed
    if not target_dir.exists():
        if stack.repo_url:
            print(f"  cloning {stack.repo_url}")
            try:
                subprocess.run(
                    ["git", "clone", stack.repo_url, str(target_dir)],
                    check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError:
                # Remote may not exist yet — init locally and add remote
                print(f"  remote not found, initializing locally")
                target_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "-C", str(target_dir), "init", "-q", "-b", "main"], check=True)
                subprocess.run(
                    ["git", "-C", str(target_dir), "remote", "add", "origin", stack.repo_url],
                    check=True,
                )
        else:
            print(f"  no STACK_REPO_{stack.name} set — generating locally only")
            target_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(target_dir), "init", "-q", "-b", "main"], check=True)

    # Apply diff
    for name in diff["added"] + diff["updated"]:
        src_skill_dir = config.skills_repo_path / name
        dst_skill_dir = target_dir / name
        dst_skill_dir.mkdir(parents=True, exist_ok=True)
        for src_file in src_skill_dir.rglob("*"):
            if src_file.is_file() and not src_file.name.startswith("."):
                rel = src_file.relative_to(src_skill_dir)
                dst = dst_skill_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)

    for name in diff["removed"]:
        rm_dir = target_dir / name
        if rm_dir.exists():
            shutil.rmtree(rm_dir)

    # Generate stack README
    readme = target_dir / "README.md"
    readme_lines = [
        f"# torus-skills-{stack.name}",
        "",
        f"Curated subset of Torus skills for the {stack.name} stack.",
        "",
        "Generated by skill-forge — do not hand-edit.",
        "",
        "## Skills in this stack",
        "",
    ]
    for skill_md in sorted(stack.skills, key=lambda p: p.parent.name):
        fm = _parse_frontmatter(skill_md)
        name = fm.get("name", skill_md.parent.name)
        desc = fm.get("description", "")
        readme_lines.append(f"- **{name}** — {desc[:120]}")
    readme.write_text("\n".join(readme_lines) + "\n", encoding="utf-8")

    # Configure git user if needed
    try:
        subprocess.run(
            ["git", "-C", str(target_dir), "config", "user.email"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(["git", "-C", str(target_dir), "config", "user.email", "skill-forge@local"], check=True)
        subprocess.run(["git", "-C", str(target_dir), "config", "user.name", "skill-forge"], check=True)

    # Commit
    subprocess.run(["git", "-C", str(target_dir), "add", "-A"], check=True)
    result = subprocess.run(
        ["git", "-C", str(target_dir), "diff", "--cached", "--quiet"],
    )
    if result.returncode != 0:
        msg = _commit_message(stack.name, diff)
        subprocess.run(["git", "-C", str(target_dir), "commit", "-q", "-m", msg], check=True)
        if stack.repo_url:
            try:
                subprocess.run(
                    ["git", "-C", str(target_dir), "push", "-u", "origin", "main"],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                print(f"  push failed: {e}")

    return diff


def _commit_message(stack_name: str, diff: dict) -> str:
    parts = []
    if diff["added"]:
        parts.append(f"add {len(diff['added'])}")
    if diff["updated"]:
        parts.append(f"update {len(diff['updated'])}")
    if diff["removed"]:
        parts.append(f"remove {len(diff['removed'])}")
    body = ", ".join(parts) or "no changes"
    return f"forge stack publish {stack_name}: {body}"


def set_skill_stacks(skill_md_path: Path, stacks: list[str]) -> bool:
    """Rewrite a SKILL.md's `stacks:` frontmatter field AND sync inline tags.

    Returns True if the file was changed, False if no change was needed.
    """
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return False

    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False

    fm_text = m.group(1)
    body_text = text[m.end():]

    new_line = f"stacks: [{', '.join(stacks)}]" if stacks else None

    fm_lines = fm_text.splitlines()
    out_lines = []
    seen_stacks_line = False
    inserted = False
    for line in fm_lines:
        if line.lstrip().startswith("stacks:"):
            seen_stacks_line = True
            if new_line:
                out_lines.append(new_line)
                inserted = True
            # otherwise skip the line — removing it
        else:
            out_lines.append(line)

    # If we have stacks and didn't find an existing line, insert before fm close
    if new_line and not inserted:
        desc_idx = next(
            (i for i, l in enumerate(out_lines) if l.lstrip().startswith("description:")),
            -1,
        )
        if desc_idx >= 0:
            out_lines.insert(desc_idx + 1, new_line)
        else:
            out_lines.append(new_line)

    new_text = "---\n" + "\n".join(out_lines) + "\n---\n" + body_text

    frontmatter_changed = new_text != text
    if frontmatter_changed:
        skill_md_path.write_text(new_text, encoding="utf-8")

    # Sync the inline banners + #stack tags. inject_stack_banners is the
    # single source of truth for both visual elements at the top of the file.
    visuals_changed = inject_stack_banners(skill_md_path, stacks)

    return frontmatter_changed or visuals_changed


# ---------- Inline Obsidian tags ---------------------------------------------

# Tags we manage are flat (#engineering, #data, etc). To find/remove them
# safely we mark them on a single line right after the frontmatter.
_STACK_TAGS_LINE_RE = re.compile(
    r"^(<!-- forge-stack-tags -->\n)(.*)$",
    re.MULTILINE,
)


def inject_stack_tags(skill_path: Path, stacks: list[str]) -> bool:
    """Sync the inline `#stack` tags on a SKILL.md to match the given list.

    Strategy: maintain a single line right after the frontmatter that holds
    the inline tags, prefixed by an HTML comment marker so we can find and
    rewrite it without disturbing user-authored content. If `stacks` is empty
    AND a marker line exists, remove the line. If the line is identical to
    what we'd write, no change.

    Returns True if the file changed.
    """
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return False

    # Find end of frontmatter
    m = re.match(r"^(---\s*\n.*?\n---\s*\n)", text, re.DOTALL)
    if not m:
        # No frontmatter — don't touch the file
        return False
    fm_end = m.end()
    before = text[:fm_end]
    after = text[fm_end:]

    desired_line = ""
    if stacks:
        tags = " ".join(f"#{s.strip()}" for s in stacks if s.strip())
        # The frontmatter regex consumes the trailing \n of the close `---`, so
        # `after` starts at the line AFTER the closer. No leading \n needed.
        # Shape: marker + tags + blank line.
        desired_line = f"<!-- forge-stack-tags -->\n{tags}\n\n"

    # Find existing marker line (if any) at the very start of `after`.
    marker_re = re.compile(
        r"^<!-- forge-stack-tags -->\n[^\n]*\n\n",
        re.DOTALL,
    )
    existing_match = marker_re.match(after)

    if existing_match:
        # Replace it
        new_after = desired_line + after[existing_match.end():]
    else:
        # Insert it
        new_after = desired_line + after

    new_text = before + new_after
    if new_text == text:
        return False
    skill_path.write_text(new_text, encoding="utf-8")
    return True


# ---------- Inline Obsidian banners -----------------------------------------

# Display info per stack. Keep in sync with the CSS snippet.
_STACK_DISPLAY = {
    "engineering": "Engineering",
    "data": "Data",
    "operations": "Operations",
}


_BANNER_BLOCK_RE = re.compile(
    r"^<!-- forge-stack-banners -->\n(?:>.*\n|\n)+",
    re.MULTILINE,
)

_TAGS_BLOCK_RE = re.compile(
    r"^<!-- forge-stack-tags -->\n[^\n]*\n+",
    re.MULTILINE,
)


def inject_stack_banners(skill_path: Path, stacks: list[str]) -> bool:
    """Sync BOTH the colored banner block AND the inline tag line at the top
    of a SKILL.md so they stay in sync.

    Strategy: strip any existing forge-managed marker blocks (banners + tags)
    from the start of the body, then rebuild them fresh in canonical order:

        ---frontmatter---

        <!-- forge-stack-banners -->
        > [!stack1] ...
        > ...

        > [!stack2] ...
        > ...

        <!-- forge-stack-tags -->
        #stack1 #stack2

        # Body...

    If `stacks` is empty, both blocks are removed.

    Returns True if the file changed.

    Note: this function takes ownership of the tags block too, so it's safe
    to call this OR inject_stack_tags but not both back-to-back from the
    same caller. set_skill_stacks calls this one only.
    """
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return False

    m = re.match(r"^(---[ \t]*\n.*?\n---[ \t]*\n)", text, re.DOTALL)
    if not m:
        return False
    fm_end = m.end()
    before = text[:fm_end]
    after = text[fm_end:]

    # Strip any leading blank lines, then any existing marker blocks (banners,
    # tags, in either order). Loop until nothing strips, so out-of-order or
    # duplicate markers all get cleaned up.
    cleaned = after.lstrip("\n")
    while True:
        prev = cleaned
        cleaned = _BANNER_BLOCK_RE.sub("", cleaned, count=1)
        cleaned = _TAGS_BLOCK_RE.sub("", cleaned, count=1)
        cleaned = cleaned.lstrip("\n")
        if cleaned == prev:
            break

    # Build desired blocks
    blocks = []
    if stacks:
        callout_blocks = []
        for stack in stacks:
            display = _STACK_DISPLAY.get(stack, stack.capitalize())
            callout_blocks.append(
                f"> [!{stack}] {display} Stack\n"
                f"> This skill is part of the {display.lower()} team\'s curated subset."
            )
        banners_block = (
            "<!-- forge-stack-banners -->\n"
            + "\n\n".join(callout_blocks)
        )
        tags = " ".join(f"#{s.strip()}" for s in stacks if s.strip())
        tags_block = f"<!-- forge-stack-tags -->\n{tags}"
        blocks.append(banners_block)
        blocks.append(tags_block)

    if blocks:
        # Separate frontmatter from blocks by one blank line, blocks from each
        # other by one blank line, blocks from body by one blank line.
        managed_section = "\n" + "\n\n".join(blocks) + "\n\n"
    else:
        managed_section = ""

    new_after = managed_section + cleaned
    new_text = before + new_after
    if new_text == text:
        return False
    skill_path.write_text(new_text, encoding="utf-8")
    return True


def skill_path_in_repo(config: Config, skill_name: str, stacks: list[str]) -> Path:
    """Where this skill should live in the canonical repo, given its stack list.

    Rules:
    - Multi-stack: alphabetically-first stack is canonical home.
    - Empty stacks: `unassigned/` subfolder.
    """
    if stacks:
        canonical = sorted(stacks)[0]
        return config.skills_repo_path / canonical / skill_name
    return config.skills_repo_path / "unassigned" / skill_name
