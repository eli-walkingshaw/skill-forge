#!/usr/bin/env python3
"""Fix link_into_claude_skills for the post-restructure nested layout.

The old function checks <repo>/<skill_name>/ but after stage 1, skills live
at <repo>/<stack>/<skill_name>/. So the path check silently fails and no
symlink gets created in ~/.claude/skills/, leaving newly-installed skills
invisible to Claude Code.

Fix: walk the stack subfolders to find the canonical home of the skill,
then symlink to that. Also backfill any missing symlinks for skills that
got installed after stage 1 but before this fix.

Run from inside ~/code/skill-forge:
    python3 fix-autolink-nested-layout.py
"""
import ast
import re
import sys
from pathlib import Path


NEW_LINK_FN = '''def link_into_claude_skills(config: Config, skill_name: str) -> None:
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
'''


def main() -> int:
    watch_path = Path("forge/watch.py")
    if not watch_path.exists():
        print("X forge/watch.py not found — run from ~/code/skill-forge")
        return 1

    s = watch_path.read_text()

    # Check if already patched (look for the new walk-the-stacks logic)
    if "Find the canonical home of this skill: walk each stack" in s:
        print("  + link_into_claude_skills already nested-layout-aware")
    else:
        # Replace the whole function. Find def link_into_claude_skills ... up to the next def/class
        start_re = re.compile(r"^def link_into_claude_skills\b", re.MULTILINE)
        m = start_re.search(s)
        if not m:
            print("X couldn't find link_into_claude_skills in watch.py")
            return 1
        next_re = re.compile(r"^(def |class )", re.MULTILINE)
        next_m = next_re.search(s, m.end())
        end = next_m.start() if next_m else len(s)
        s = s[:m.start()] + NEW_LINK_FN + "\n\n" + s[end:]
        watch_path.write_text(s)
        print("  + replaced link_into_claude_skills with nested-layout-aware version")

    # Parse-check
    try:
        ast.parse(watch_path.read_text())
        print("  + watch.py parses cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    # Clear .pyc
    pycache = Path("forge/__pycache__")
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    # ---- Backfill: find skills in the repo without a ~/.claude/skills/ symlink ----
    print()
    print("Backfilling missing ~/.claude/skills/ symlinks...")

    # Load .env to get SKILLS_REPO_PATH
    env_vars = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
    repo_path_str = env_vars.get("SKILLS_REPO_PATH")
    if not repo_path_str:
        print("  ! SKILLS_REPO_PATH not in .env — skipping backfill")
        return 0
    repo = Path(repo_path_str).expanduser()

    claude_skills = Path.home() / ".claude" / "skills"
    if not claude_skills.exists():
        print(f"  ! ~/.claude/skills/ doesn't exist — skipping backfill")
        return 0

    # Walk stack subfolders for canonical skills (real dirs, not symlinks)
    canonical_skills: dict[str, Path] = {}
    for stack_dir in sorted(repo.iterdir()):
        if not stack_dir.is_dir() or stack_dir.name.startswith("."):
            continue
        for skill_dir in sorted(stack_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if skill_dir.is_symlink():
                continue  # secondary-stack symlink, not the canonical home
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            canonical_skills[skill_dir.name] = skill_dir

    # Compare against existing symlinks
    backfilled = 0
    fixed = 0
    for name, canonical_path in canonical_skills.items():
        link_path = claude_skills / name
        if link_path.is_symlink():
            existing = link_path.resolve()
            if existing == canonical_path.resolve():
                continue  # correct, nothing to do
            # Wrong target — fix it
            link_path.unlink()
            link_path.symlink_to(canonical_path)
            print(f"  fixed:      {name} -> {canonical_path}")
            fixed += 1
        elif link_path.exists():
            print(f"  skipped:    {name} (exists, not a symlink)")
        else:
            link_path.symlink_to(canonical_path)
            print(f"  backfilled: {name} -> {canonical_path}")
            backfilled += 1

    print()
    if backfilled or fixed:
        print(f"+ {backfilled} new symlink(s), {fixed} corrected")
        print()
        print("Open a fresh Claude Code session and `/skills` should show the new ones.")
    else:
        print("+ all skills already correctly linked (nothing to backfill)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
