"""Watch daemon: when a proposal is moved to approved/, sync it to the skills repo."""
from __future__ import annotations
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config
from .stacks import _is_blessed_stack


PROPOSAL_DIVIDER_RE = re.compile(r"^---\s*$", re.MULTILINE)


def extract_skill_md(proposal_text: str) -> str | None:
    """A proposal note wraps the real SKILL.md after a divider. Strip the wrapper.

    Proposals can have an optional wrapper frontmatter block at the very top
    (used for lifecycle tags like `tags: [pending]`), then a callout, then a
    `---` divider, then the real SKILL.md.

    Structure:
        ---                    (optional wrapper frontmatter)
        tags: [pending]
        ---
        > [!info] ...
        > ...
        ---            <-- divider between wrapper and skill
        ---            <-- start of skill frontmatter
        name: ...
        ---
        # ...

    Tricky: we have to distinguish "this is a wrapped proposal" from "this is
    just a SKILL.md directly" (also starts with `---`). Wrapper frontmatter
    only contains lifecycle metadata (tags), not skill fields like name/description.
    If the first `---...---` block contains `name:`, it IS the skill, not wrapper.
    """
    text = proposal_text

    # Look at the first frontmatter block, if any
    fm_match = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", text, re.DOTALL)
    if fm_match:
        fm_body = fm_match.group(1)
        # If this frontmatter has name: / description:, it's the skill itself
        # (file is a bare SKILL.md, no wrapper). Don't consume it.
        is_skill_frontmatter = bool(
            re.search(r"^name\s*:", fm_body, re.MULTILINE)
            or re.search(r"^description\s*:", fm_body, re.MULTILINE)
        )
        if not is_skill_frontmatter:
            # It's wrapper frontmatter — skip past it
            text = text[fm_match.end():]

    # Now look for the divider between wrapper-callout and skill
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        # No divider — maybe the user wrote the SKILL.md directly without wrapper
        if text.lstrip().startswith("---"):
            return text
        return None
    return parts[1].strip()




def _rewrite_wrapper_tags(path: Path, new_tags: list[str]) -> None:
    """Rewrite the wrapper-frontmatter `tags:` field of a proposal file.

    If the file has wrapper frontmatter at the very top, find the `tags:` line
    and replace its value. If no wrapper frontmatter, prepend one with the tags.
    Quietly no-ops if the file is missing or unreadable.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    tag_list = "[" + ", ".join(new_tags) + "]"

    fm_match = re.match(r"^(---[ \t]*\n)(.*?)(\n---[ \t]*\n)", text, re.DOTALL)
    if fm_match:
        # Wrapper frontmatter exists — rewrite its `tags:` line, or add it
        fm_body = fm_match.group(2)
        if re.search(r"^tags\s*:", fm_body, re.MULTILINE):
            new_body = re.sub(
                r"^tags\s*:.*$",
                f"tags: {tag_list}",
                fm_body,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            new_body = fm_body.rstrip() + f"\ntags: {tag_list}"
        new_text = fm_match.group(1) + new_body + fm_match.group(3) + text[fm_match.end():]
    else:
        # No wrapper frontmatter — prepend one
        new_text = f"---\ntags: {tag_list}\n---\n\n" + text

    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        pass
SKILL_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def validate_skill(skill_md: str) -> tuple[bool, str, str]:
    """Returns (ok, error_msg, skill_name)."""
    m = SKILL_FRONTMATTER_RE.match(skill_md)
    if not m:
        return False, "missing frontmatter block", ""
    fm = m.group(1)
    name_match = NAME_RE.search(fm)
    if not name_match:
        return False, "missing `name:` in frontmatter", ""
    name = name_match.group(1).strip()
    if not re.match(r"^[a-z][a-z0-9-]*$", name):
        return False, f"name '{name}' must be kebab-case lowercase", name
    if "description:" not in fm:
        return False, "missing `description:` in frontmatter", name
    return True, "", name


def install_skill(config: Config, skill_name: str, skill_md: str) -> Path:
    """Write a skill into the canonical repo.

    Two paths:
      - Captured skill (no forge_source): write SKILL.md to <stack>/<name>/SKILL.md
      - Imported skill (has forge_source): copy whole upstream directory
        including references/, scripts/, assets/, and LICENSE (if present)

    The stack is read from the SKILL.md's own frontmatter. Multi-stack
    skills get a canonical home in the alphabetically-first stack, with
    symlinks from secondary stacks.
    """
    # Parse frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", skill_md, re.DOTALL)
    stacks = []
    forge_source = ""
    never_publish = False
    if fm_match:
        fm = fm_match.group(1)
        stacks_line = re.search(r"^stacks\s*:\s*\[(.*?)\]\s*$", fm, re.MULTILINE)
        if stacks_line:
            stacks = [s.strip().strip(chr(34) + chr(39) + " ") for s in stacks_line.group(1).split(",")]
            stacks = [s for s in stacks if s]
        source_line = re.search(r"^forge_source\s*:\s*(.+?)\s*$", fm, re.MULTILINE)
        if source_line:
            forge_source = source_line.group(1).strip()
        np_line = re.search(r"^never_publish\s*:\s*(true|True|1)\s*$", fm, re.MULTILINE)
        if np_line:
            never_publish = True

    # Pick canonical subfolder
    if never_publish or not stacks:
        canonical_dir = config.skills_repo_path / "unassigned" / skill_name
    else:
        canonical_stack = sorted(stacks)[0]
        canonical_dir = config.skills_repo_path / canonical_stack / skill_name

    canonical_dir.mkdir(parents=True, exist_ok=True)

    if forge_source:
        # IMPORT path: copy the whole upstream skill directory
        target = _install_imported_skill(
            canonical_dir=canonical_dir,
            skill_md_text=skill_md,
            forge_source=forge_source,
        )
    else:
        # CAPTURE path: just write the SKILL.md
        target = canonical_dir / "SKILL.md"
        target.write_text(skill_md, encoding="utf-8")

    # Maintain symlinks for secondary stacks
    if not never_publish and len(stacks) >= 2:
        _sync_secondary_stack_symlinks(config, skill_name, stacks)

    return target


def _install_imported_skill(*, canonical_dir: Path, skill_md_text: str, forge_source: str) -> Path:
    """Install an imported skill: copy the upstream skill dir + LICENSE.

    forge_source format: `<url>@<short-sha>:<path-to-SKILL.md-from-repo-root>`

    We locate the upstream clone by name (the subscription whose URL matches),
    find the skill directory (parent of the SKILL.md path), and copy it.
    """
    # Parse: url@sha:path
    m = re.match(r"^(.+?)@([a-f0-9]+):(.+)$", forge_source)
    if not m:
        # Malformed forge_source — fall back to just writing SKILL.md
        target = canonical_dir / "SKILL.md"
        target.write_text(skill_md_text, encoding="utf-8")
        print(f"[watch] forge_source is malformed; wrote SKILL.md only")
        return target

    source_url = m.group(1).strip()
    source_sha = m.group(2).strip()
    rel_skill_path = Path(m.group(3).strip())

    # Find the subscription whose URL matches
    from .subscriptions import list_subscriptions
    subs = list_subscriptions()
    matching = [s for s in subs if s.url == source_url]
    if not matching:
        print(f"[watch] no subscription matches source URL — wrote SKILL.md only")
        target = canonical_dir / "SKILL.md"
        target.write_text(skill_md_text, encoding="utf-8")
        return target

    sub = matching[0]
    upstream_skill_dir = sub.clone_path / rel_skill_path.parent
    if not upstream_skill_dir.exists():
        print(f"[watch] upstream skill dir missing: {upstream_skill_dir}")
        target = canonical_dir / "SKILL.md"
        target.write_text(skill_md_text, encoding="utf-8")
        return target

    # Copy everything from the upstream skill dir into canonical_dir.
    # Skip dotfiles and __pycache__ WITHIN the skill dir (don't accidentally filter
    # based on the clone's parent path containing .skill-forge etc.)
    copied = 0
    for src_path in upstream_skill_dir.rglob("*"):
        rel = src_path.relative_to(upstream_skill_dir)
        # Filter based on the relative path's parts only
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        dst = canonical_dir / rel
        if src_path.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif src_path.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst)
            copied += 1

    # Now overwrite the SKILL.md with our annotated version (which has stacks: + forge_source:)
    skill_md_target = canonical_dir / "SKILL.md"
    skill_md_target.write_text(skill_md_text, encoding="utf-8")

    # Also copy the upstream LICENSE file (if present) into the skill dir.
    # Search the clone root for LICENSE / LICENSE.txt / LICENSE.md.
    for license_name in ("LICENSE", "LICENSE.txt", "LICENSE.md"):
        upstream_license = sub.clone_path / license_name
        if upstream_license.exists():
            shutil.copy2(upstream_license, canonical_dir / license_name)
            copied += 1
            break

    print(f"[watch] imported {copied} file(s) from upstream skill dir")
    return skill_md_target


def _sync_secondary_stack_symlinks(config: Config, skill_name: str, stacks: list[str]) -> None:
    """For multi-stack skills, ensure symlinks exist from secondary stacks
    pointing at the canonical home.
    """
    if len(stacks) < 2:
        return
    canonical_stack = sorted(stacks)[0]
    canonical_path = config.skills_repo_path / canonical_stack / skill_name
    for stack in stacks:
        if stack == canonical_stack:
            continue
        secondary_dir = config.skills_repo_path / stack
        secondary_dir.mkdir(parents=True, exist_ok=True)
        symlink = secondary_dir / skill_name
        if symlink.exists() or symlink.is_symlink():
            if symlink.is_symlink() and symlink.resolve() == canonical_path.resolve():
                continue  # already correct
            try:
                if symlink.is_symlink():
                    symlink.unlink()
                else:
                    shutil.rmtree(symlink)
            except OSError:
                continue
        rel_target = Path("..") / canonical_stack / skill_name
        try:
            symlink.symlink_to(rel_target)
        except OSError as e:
            print(f"[watch] couldn't symlink {stack}/{skill_name} -> ../{canonical_stack}/{skill_name}: {e}")


def git_commit_and_push(config: Config, message: str, paths: list[Path]) -> None:
    repo = config.skills_repo_path
    if not (repo / ".git").exists():
        print(f"[watch] {repo} is not a git repo; skipping commit")
        return

    rel_paths = [str(p.relative_to(repo)) for p in paths]
    subprocess.run(["git", "-C", str(repo), "add", *rel_paths], check=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
    )
    if result.returncode == 0:
        print("[watch] nothing to commit")
        return

    subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True)
    if config.git_auto_push:
        subprocess.run(
            ["git", "-C", str(repo), "push", config.git_remote, config.git_branch],
            check=True,
        )


def link_into_claude_skills(config: Config, skill_name: str) -> None:
    """Create ~/.claude/skills/<skill_name> -> <repo>/<stack>/<skill_name>.

    After the stage 1 restructure, skills live under stack subfolders
    (data/, engineering/, operations/, unassigned/) rather than at the repo
    root. This function finds the canonical home by walking the stack folders
    and symlinks ~/.claude/skills/ to whichever location actually contains
    the skill.

    Idempotent: skips if a correct symlink is already in place, leaves
    existing files alone.
    """
    from pathlib import Path
    claude_skills = Path.home() / ".claude" / "skills"
    if not claude_skills.exists():
        # Claude Code isn't installed (or uses a different location); nothing to do.
        return

    # Find the canonical home of this skill: walk each stack subfolder for
    # a real directory (not a symlink) whose name matches skill_name.
    target = None
    repo = config.skills_repo_path
    if repo.exists():
        for stack_dir in repo.iterdir():
            if not _is_blessed_stack(stack_dir.name):
                continue
            if not stack_dir.is_dir() or stack_dir.name.startswith("."):
                continue
            candidate = stack_dir / skill_name
            if candidate.is_dir() and not candidate.is_symlink():
                # Found canonical home (real folder, not the symlink-from-secondary)
                target = candidate
                break
        # Fallback: maybe it's at the flat layout (pre-restructure)
        if target is None:
            flat = repo / skill_name
            if flat.is_dir():
                target = flat

    if target is None or not target.exists():
        print(f"[watch] symlink skipped — couldn't find skill `{skill_name}` in repo")
        return

    link_path = claude_skills / skill_name
    if link_path.is_symlink():
        existing = link_path.resolve()
        if existing == target.resolve():
            print(f"[watch] symlink already correct: {link_path.name}")
            return
        # Wrong target — replace it
        link_path.unlink()
        try:
            link_path.symlink_to(target)
            print(f"[watch] updated symlink: {link_path.name} -> {target}")
        except OSError as e:
            print(f"[watch] symlink update failed: {e}")
        return
    if link_path.exists():
        print(f"[watch] {link_path.name} exists and is not a symlink — leaving it alone")
        return
    try:
        link_path.symlink_to(target)
        print(f"[watch] linked: {link_path.name} -> {target}")
    except OSError as e:
        print(f"[watch] symlink failed: {e}")


def inject_tags_into_callout(text: str, tags: list[str]) -> str:
    """Insert an Obsidian-style tag line into the `> [!info]` callout block.

    The first line of a proposal looks like `> [!info] skill-forge proposal`.
    We insert a tag line immediately after it: `> #tag1 #tag2`. If the tags
    are already present, this is a no-op (idempotent).
    """
    if not tags:
        return text

    lines = text.splitlines()
    if not lines or not lines[0].lstrip().startswith("> [!info]"):
        # No callout to inject into — return unchanged.
        return text

    # Check if any of the tags are already present anywhere in the callout.
    # Find the end of the callout (first non-`>`-prefixed line).
    callout_end = 1
    while callout_end < len(lines) and (
        lines[callout_end].startswith(">") or lines[callout_end].strip() == ""
    ):
        if not lines[callout_end].startswith(">") and lines[callout_end].strip() == "":
            break
        callout_end += 1
    callout_text = "\n".join(lines[:callout_end])

    # Idempotent: if the exact tag line is already there, skip.
    tag_line = "> " + " ".join(tags)
    for line in lines[:callout_end]:
        if line.strip() == tag_line.strip():
            return text  # already present

    # Insert the tag line right after the `> [!info]` line.
    new_lines = [lines[0], tag_line] + lines[1:]
    result = "\n".join(new_lines)
    if text.endswith("\n"):
        result += "\n"
    return result


def process_approved_file(config: Config, path: Path) -> None:
    """Process a single approved proposal file."""
    print(f"[watch] processing {path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[watch] read failed: {e}")
        return

    skill_md = extract_skill_md(text)
    if skill_md is None:
        print(f"[watch] couldn't find SKILL.md content in {path.name}")
        return

    ok, err, name = validate_skill(skill_md)
    if not ok:
        print(f"[watch] validation failed for {path.name}: {err}")
        return

    # ---- Gates: run quality/sensitivity/effectiveness checks before install ----
    import os as _os
    gates_enabled = _os.environ.get("GATES_ENABLED", "true").lower() == "true"
    if gates_enabled:
        from .gates import run_all_gates
        block_thin = _os.environ.get("GATES_BLOCK_THIN_DRAFTS", "true").lower() == "true"
        effect_enabled = _os.environ.get("GATES_EFFECTIVENESS_ENABLED", "true").lower() == "true"
        report = run_all_gates(
            skill_md,
            skill_name=name,
            block_thin_drafts=block_thin,
            effectiveness_enabled=effect_enabled,
            api_key=config.anthropic_api_key,
        )
        if not report.overall_passed:
            rejected_dir = config.vault_path / "rejected"
            rejected_dir.mkdir(parents=True, exist_ok=True)
            rejected_path = rejected_dir / path.name
            report_path = rejected_dir / (path.stem + ".gate-report.md")
            report_path.write_text(report.render(), encoding="utf-8")
            # Tag the proposal file with #rejected #gate-failed for Obsidian browsing.
            _rewrite_wrapper_tags(path, ['rejected', 'gate-failed'])
            try:
                tagged = inject_tags_into_callout(
                    path.read_text(encoding="utf-8"),
                    ["#rejected", "#gate-failed"],
                )
                path.write_text(tagged, encoding="utf-8")
            except OSError:
                pass
            shutil.move(str(path), str(rejected_path))
            print(f"[watch] gates failed for {path.name} — moved to rejected/")
            print(f"[watch] see {report_path.name} for details")
            return
        print(f"[watch] gates passed ({len(report.results)} checks)")

    target = install_skill(config, name, skill_md)
    print(f"[watch] installed → {target}")

    try:
        git_commit_and_push(
            config,
            message=f"forge: add skill `{name}` (from {path.name})",
            paths=[target],
        )
        print(f"[watch] committed{' + pushed' if config.git_auto_push else ''}")
    except subprocess.CalledProcessError as e:
        print(f"[watch] git operation failed: {e}")
        return

    # Symlink the new skill into ~/.claude/skills/ so Claude Code picks it up.
    link_into_claude_skills(config, name)

    # Tag the approved proposal with #archived #installed before moving.
    _rewrite_wrapper_tags(path, ['installed'])
    try:
        tagged = inject_tags_into_callout(
            path.read_text(encoding="utf-8"),
            ["#archived", "#installed"],
        )
        path.write_text(tagged, encoding="utf-8")
    except OSError:
        pass

    archive_target = config.archive_dir / f"installed__{path.name}"
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), str(archive_target))
        print(f"[watch] archived → {archive_target.name}")
    except FileNotFoundError:
        print(f"[watch] (proposal already gone from approved/, skipping archive)")


def watch_loop(config: Config, poll_interval: float = 2.0) -> None:
    """Poll-based watcher. No external deps, works everywhere."""
    approved = config.approved_dir
    approved.mkdir(parents=True, exist_ok=True)
    print(f"[watch] watching {approved} (poll every {poll_interval}s)")

    seen: set[str] = set()
    while True:
        try:
            # Snapshot the directory listing before any moves happen.
            files = sorted(approved.glob("*.md"))
            for f in files:
                # Skip archive leftovers — these are already-installed files
                # that ended up back in approved/ somehow. Never re-process.
                if f.name.startswith("installed__"):
                    continue
                # Skip files that have disappeared (e.g. moved by a prior
                # iteration's archive step, or by the user).
                try:
                    mtime = f.stat().st_mtime
                except FileNotFoundError:
                    continue
                key = f"{f.name}:{mtime}"
                if key in seen:
                    continue
                seen.add(key)
                process_approved_file(config, f)
        except Exception as e:
            print(f"[watch] error: {e}")
        time.sleep(poll_interval)
