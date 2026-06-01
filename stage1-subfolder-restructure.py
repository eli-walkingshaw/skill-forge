#!/usr/bin/env python3
"""Stage 1: Subfolder restructure.

Reorganizes ~/code/torus-skills/ from a flat layout:

    torus-skills/
    ├── suiteql-cookbook/SKILL.md      (stacks: [data, engineering])
    ├── netsuite-suitelet-scaffolding/SKILL.md
    └── ...

Into a stack-keyed nested layout:

    torus-skills/
    ├── data/                          (canonical home for skills whose
    │   ├── suiteql-cookbook/SKILL.md       first stack alphabetically is `data`)
    │   └── ...
    ├── engineering/
    │   ├── suiteql-cookbook → ../data/suiteql-cookbook  (symlink, because the
    │   │                                                 skill is in two stacks)
    │   ├── netsuite-suitelet-scaffolding/SKILL.md
    │   └── ...
    ├── operations/
    │   └── ...
    └── unassigned/                    (skills with no stacks: field)
        └── ...

Rules:
- For multi-stack skills, the alphabetically-first stack is the canonical home;
  symlinks point from secondary stacks.
- Skills with `never_publish: true` stay in `unassigned/` (or get an explicit
  `never_publish/` subfolder — we use `unassigned` since they're functionally
  the same to the layout: not in any stack).
- Skills with no `stacks:` field go to `unassigned/`.
- Uses `git mv` so history is preserved.

Also patches forge code to use the new nested layout:
- watch.py:install_skill writes to <repo>/<stack>/<name>/SKILL.md
- stacks.py:discover_stacks walks the nested structure
- stacks.py:list_skill_assignments walks the nested structure
- Adds a `skill_path_in_repo(config, name, stacks)` helper

Re-creates the symlinks in ~/.claude/skills/ to point at the canonical homes.

Run from inside ~/code/skill-forge:
    python3 stage1-subfolder-restructure.py

This patch is read-only by default. To actually perform the restructure, run:
    python3 stage1-subfolder-restructure.py --apply
"""
import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ---- The forge code patches we install ----

# A new helper added to stacks.py — returns the canonical home path for a skill.
SKILL_PATH_HELPER = '''

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
'''


# Replacement install_skill that uses the nested layout.
NEW_INSTALL_SKILL = '''def install_skill(config: Config, skill_name: str, skill_md: str) -> Path:
    """Write the SKILL.md into the canonical repo at <repo>/<stack>/<name>/SKILL.md.

    The stack is read from the SKILL.md's own frontmatter. Skills with multiple
    stacks land in the alphabetically-first stack as canonical home; secondary
    stacks get symlinks (handled by sync_stack_symlinks, run after install).
    Skills with no stacks: field go to `unassigned/`.
    """
    # Parse the stacks: field from the SKILL.md being installed
    fm_match = re.match(r"^---\\s*\\n(.*?)\\n---\\s*\\n", skill_md, re.DOTALL)
    stacks = []
    if fm_match:
        fm = fm_match.group(1)
        stacks_line = re.search(r"^stacks\\s*:\\s*\\[(.*?)\\]\\s*$", fm, re.MULTILINE)
        if stacks_line:
            stacks = [s.strip().strip(chr(34) + chr(39) + " ") for s in stacks_line.group(1).split(",")]
            stacks = [s for s in stacks if s]

    # Pick canonical subfolder
    if stacks:
        canonical_stack = sorted(stacks)[0]
        skill_dir = config.skills_repo_path / canonical_stack / skill_name
    else:
        skill_dir = config.skills_repo_path / "unassigned" / skill_name

    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    target.write_text(skill_md, encoding="utf-8")

    # Maintain symlinks for secondary stacks
    _sync_secondary_stack_symlinks(config, skill_name, stacks)

    return target


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
        # Use relative path so the symlink works across machines
        rel_target = Path("..") / canonical_stack / skill_name
        try:
            symlink.symlink_to(rel_target)
        except OSError as e:
            print(f"[watch] couldn't symlink {stack}/{skill_name} -> ../{canonical_stack}/{skill_name}: {e}")
'''


# Replacement discover_stacks/list_skill_assignments that walk the nested layout.
NEW_DISCOVER = '''def discover_stacks(config: Config) -> dict[str, Stack]:
    """Walk the canonical skills repo (nested layout) and group skills by stack.

    Expected layout: <repo>/<stack>/<skill-name>/SKILL.md
    Skills with `never_publish: true` are excluded.
    """
    repo = config.skills_repo_path
    if not repo.exists():
        return {}

    stacks: dict[str, Stack] = {}
    for stack_dir in sorted(repo.iterdir()):
        if not stack_dir.is_dir() or stack_dir.name.startswith("."):
            continue
        # Skip 'unassigned' and any non-stack folders.
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
'''


def parse_skill_frontmatter(skill_md_path):
    """Parse a SKILL.md's frontmatter. Returns dict of key->value(s)."""
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", text, re.DOTALL)
    if not m:
        return {}
    fm = m.group(1)
    out = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        list_m = re.match(r"\[(.*?)\]", val)
        if list_m:
            items = [t.strip().strip("\"'` ") for t in list_m.group(1).split(",")]
            out[key] = [i for i in items if i]
        elif val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        else:
            out[key] = val
    return out


def plan_restructure(repo_path):
    """Read the current flat repo and plan the restructure.

    Returns list of (source_path, target_relative_path, action) tuples
    where action is "move" or "symlink".
    """
    plan = []
    seen = set()

    for child in sorted(repo_path.iterdir()):
        # Skip hidden, root files, and already-nested stack folders
        if not child.is_dir() or child.name.startswith("."):
            continue
        # If this looks like a stack folder (contains skill subdirs that have
        # SKILL.md inside), skip it — already restructured
        if child.name in ("engineering", "data", "operations", "unassigned"):
            # Already nested; check if any skill from there overlaps with
            # our flat-style scanning. Skip these from the move plan.
            continue

        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        if child.name in seen:
            continue
        seen.add(child.name)

        fm = parse_skill_frontmatter(skill_md)
        stacks = fm.get("stacks") or []
        if isinstance(stacks, str):
            stacks = [stacks]
        never_publish = fm.get("never_publish", False)

        if never_publish or not stacks:
            target = Path("unassigned") / child.name
            plan.append((child, target, "move"))
        else:
            canonical_stack = sorted(stacks)[0]
            canonical_target = Path(canonical_stack) / child.name
            plan.append((child, canonical_target, "move"))
            for s in stacks:
                if s == canonical_stack:
                    continue
                symlink_target = Path(s) / child.name
                plan.append((child, symlink_target, "symlink", canonical_target))

    return plan


def run_git(repo_path, *args, check=True):
    """Run a git command in the repo and return its result."""
    cmd = ["git", "-C", str(repo_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def apply_restructure(repo_path, plan, dry_run=True):
    """Apply the restructure plan via git mv + symlink ln -s."""
    if not (repo_path / ".git").exists():
        print(f"X {repo_path} is not a git repo, can't proceed safely")
        return False

    # First make sure the working tree is clean
    result = run_git(repo_path, "status", "--porcelain", check=False)
    if result.stdout.strip():
        print(f"X {repo_path} has uncommitted changes; commit or stash first:")
        print(result.stdout)
        return False

    if dry_run:
        print("=== DRY RUN === (use --apply to actually do this)")
    print()

    # Create stack subfolder structures first
    needed_dirs = set()
    for entry in plan:
        target = entry[1]
        needed_dirs.add(target.parent)
    for d in sorted(needed_dirs):
        full = repo_path / d
        if not full.exists():
            print(f"  mkdir {d}")
            if not dry_run:
                full.mkdir(parents=True, exist_ok=True)

    # Do the moves first
    for entry in plan:
        if entry[2] != "move":
            continue
        source, target, _ = entry
        rel_source = source.relative_to(repo_path)
        print(f"  git mv {rel_source} {target}")
        if not dry_run:
            result = run_git(repo_path, "mv", str(rel_source), str(target), check=False)
            if result.returncode != 0:
                print(f"    X git mv failed: {result.stderr.strip()}")
                return False

    # Then create symlinks for secondary stacks
    for entry in plan:
        if entry[2] != "symlink":
            continue
        _, symlink_path, _, canonical_target = entry
        full_symlink = repo_path / symlink_path
        rel_target = Path("..") / canonical_target
        print(f"  ln -s {rel_target} {symlink_path}")
        if not dry_run:
            if full_symlink.exists() or full_symlink.is_symlink():
                continue
            full_symlink.symlink_to(rel_target)
            # Add the symlink to git
            run_git(repo_path, "add", str(symlink_path))

    return True


def patch_forge_code(forge_dir):
    """Update install_skill, discover_stacks, and add skill_path_in_repo."""
    changes = []

    # ---- watch.py: replace install_skill ----
    watch_path = forge_dir / "watch.py"
    s = watch_path.read_text()
    if "_sync_secondary_stack_symlinks" in s:
        print("  + watch.py:install_skill already nested-aware")
    else:
        # Find install_skill and replace it
        start_re = re.compile(r"^def install_skill\b", re.MULTILINE)
        m = start_re.search(s)
        if not m:
            print("X couldn't find install_skill in watch.py")
            return False
        # Find end (next top-level def)
        next_re = re.compile(r"^(def |class )", re.MULTILINE)
        next_m = next_re.search(s, m.end())
        end = next_m.start() if next_m else len(s)
        s = s[:m.start()] + NEW_INSTALL_SKILL + "\n\n" + s[end:]
        watch_path.write_text(s)
        changes.append("+ watch.py:install_skill replaced (nested layout aware)")

    # ---- stacks.py: replace discover_stacks + list_skill_assignments ----
    stacks_path = forge_dir / "stacks.py"
    s = stacks_path.read_text()
    # Check if already patched
    if "seen_canonical: set[Path]" in s:
        print("  + stacks.py already nested-aware")
    else:
        # Replace discover_stacks through end of list_skill_assignments
        start_re = re.compile(r"^def discover_stacks\b", re.MULTILINE)
        m = start_re.search(s)
        if not m:
            print("X couldn't find discover_stacks in stacks.py")
            return False
        # Find the next def AFTER list_skill_assignments
        # Walk forward through "def list_skill_assignments" then to the def after
        list_re = re.compile(r"^def list_skill_assignments\b", re.MULTILINE)
        lm = list_re.search(s, m.end())
        if not lm:
            print("X couldn't find list_skill_assignments in stacks.py")
            return False
        next_def_re = re.compile(r"^def ", re.MULTILINE)
        next_m = next_def_re.search(s, lm.end())
        end = next_m.start() if next_m else len(s)
        s = s[:m.start()] + NEW_DISCOVER + "\n\n" + s[end:]
        stacks_path.write_text(s)
        changes.append("+ stacks.py:discover_stacks/list_skill_assignments replaced")

    # ---- stacks.py: add skill_path_in_repo helper if missing ----
    s = stacks_path.read_text()
    if "def skill_path_in_repo" in s:
        print("  + stacks.py:skill_path_in_repo already present")
    else:
        s = s.rstrip() + "\n" + SKILL_PATH_HELPER
        stacks_path.write_text(s)
        changes.append("+ stacks.py:skill_path_in_repo added")

    # Parse-check
    try:
        ast.parse(watch_path.read_text())
        ast.parse(stacks_path.read_text())
    except SyntaxError as e:
        print(f"X syntax error after patch: {e}")
        return False

    for c in changes:
        print(f"  {c}")

    # Clear .pyc cache
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()

    return True


def recreate_claude_symlinks(repo_path, dry_run=True):
    """Update ~/.claude/skills/ symlinks to point at the new nested paths."""
    claude_dir = Path.home() / ".claude" / "skills"
    if not claude_dir.exists():
        print("  (no ~/.claude/skills/ — skipping symlink update)")
        return

    print()
    print("Updating ~/.claude/skills/ symlinks for the new layout...")

    # For each skill, find its canonical home in the restructured repo
    # (resolve symlinks to find the actual home folder).
    skills_to_link: dict[str, Path] = {}
    for stack_dir in repo_path.iterdir():
        if not stack_dir.is_dir() or stack_dir.name.startswith("."):
            continue
        for skill_dir in stack_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.is_symlink():
                # Skip — secondary location, follow it to find canonical
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            skills_to_link[skill_dir.name] = skill_dir

    # Remove existing symlinks for skills we're about to relink
    for existing in claude_dir.iterdir():
        if existing.is_symlink() and existing.name in skills_to_link:
            print(f"  rm {existing}")
            if not dry_run:
                existing.unlink()

    # Create new symlinks
    for name, canonical_path in sorted(skills_to_link.items()):
        link_path = claude_dir / name
        if link_path.exists() or link_path.is_symlink():
            continue
        print(f"  ln -s {canonical_path} {link_path}")
        if not dry_run:
            link_path.symlink_to(canonical_path)


def main():
    apply_mode = "--apply" in sys.argv

    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge")
        return 1

    # Load .env to find SKILLS_REPO_PATH
    env_vars = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
    repo_path_str = env_vars.get("SKILLS_REPO_PATH")
    if not repo_path_str:
        print("X SKILLS_REPO_PATH not in .env — can't proceed")
        return 1
    repo_path = Path(repo_path_str).expanduser()
    if not repo_path.exists():
        print(f"X repo path {repo_path} doesn't exist")
        return 1

    print(f"Canonical repo: {repo_path}")
    print(f"Mode:           {'APPLY' if apply_mode else 'DRY RUN (use --apply to actually do this)'}")
    print()

    # Build plan
    print("Planning restructure...")
    plan = plan_restructure(repo_path)
    if not plan:
        print("  (nothing to restructure — repo may already be nested, or empty)")
        # Still patch forge code in case we need to
        print()
        print("Patching forge code anyway...")
        if not patch_forge_code(forge_dir):
            return 1
        return 0

    print(f"  {len(plan)} action(s) planned:")
    moves = [p for p in plan if p[2] == "move"]
    links = [p for p in plan if p[2] == "symlink"]
    print(f"    {len(moves)} skill folder(s) to move")
    print(f"    {len(links)} symlink(s) for multi-stack skills")
    print()

    # Apply the restructure
    ok = apply_restructure(repo_path, plan, dry_run=not apply_mode)
    if not ok:
        return 1

    if not apply_mode:
        print()
        print("=== this was a DRY RUN ===")
        print("Re-run with --apply to actually perform the restructure.")
        return 0

    # Patch forge code
    print()
    print("Patching forge code...")
    if not patch_forge_code(forge_dir):
        return 1

    # Re-create ~/.claude/skills/ symlinks
    recreate_claude_symlinks(repo_path, dry_run=False)

    # Commit the restructure
    print()
    print("Committing restructure to repo...")
    run_git(repo_path, "add", "-A")
    result = run_git(repo_path, "diff", "--cached", "--quiet", check=False)
    if result.returncode == 0:
        print("  (nothing to commit)")
    else:
        run_git(repo_path, "commit", "-m", "forge: restructure into stack subfolders")
        print("  + committed")
        # Push if configured
        if env_vars.get("GIT_AUTO_PUSH", "true").lower() == "true":
            push_result = run_git(repo_path, "push", check=False)
            if push_result.returncode == 0:
                print("  + pushed to origin")
            else:
                err = push_result.stderr.strip().splitlines()
                first_line = err[0] if err else "unknown"
                print(f"  - push skipped: {first_line}")
                print(f"    (you can push manually: cd {repo_path} && git push)")

    print()
    print("+ stage 1 complete")
    print()
    print("New layout:")
    for stack_dir in sorted(repo_path.iterdir()):
        if not stack_dir.is_dir() or stack_dir.name.startswith("."):
            continue
        count = sum(1 for c in stack_dir.iterdir() if c.is_dir())
        link_count = sum(1 for c in stack_dir.iterdir() if c.is_symlink())
        canonical_count = count - link_count
        print(f"  {stack_dir.name}/  ({canonical_count} canonical, {link_count} symlinked)")
    print()
    print("Restart the watcher so it picks up the new install_skill:")
    print("  pkill -9 -f 'forge watch'")
    print("  cd ~/code/skill-forge")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    return 0


if __name__ == "__main__":
    sys.exit(main())
