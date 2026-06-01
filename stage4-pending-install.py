#!/usr/bin/env python3
"""Stage 4: extend the watcher to handle imported skills from pending/.

When a proposal lands in approved/ with a `forge_source:` field in its
frontmatter, it's an IMPORT (came from `forge sync`, sitting in pending/).
The install behavior needs to be different:

  - Capture flow (no forge_source): write SKILL.md to <stack>/<name>/SKILL.md
  - Import flow (forge_source present): COPY the whole skill directory
    from the upstream clone into <stack>/<name>/, including references/,
    scripts/, assets/, and LICENSE file (if upstream has one)

Also adds a small `pending/` scaffolding step on startup so the folder exists.

Run from inside ~/code/skill-forge:
    python3 stage4-pending-install.py
"""
import ast
import re
import sys
from pathlib import Path


# The replacement install_skill — knows the difference between captured and imported.
NEW_INSTALL_SKILL = '''def install_skill(config: Config, skill_name: str, skill_md: str) -> Path:
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
    fm_match = re.match(r"^---\\s*\\n(.*?)\\n---\\s*\\n", skill_md, re.DOTALL)
    stacks = []
    forge_source = ""
    never_publish = False
    if fm_match:
        fm = fm_match.group(1)
        stacks_line = re.search(r"^stacks\\s*:\\s*\\[(.*?)\\]\\s*$", fm, re.MULTILINE)
        if stacks_line:
            stacks = [s.strip().strip(chr(34) + chr(39) + " ") for s in stacks_line.group(1).split(",")]
            stacks = [s for s in stacks if s]
        source_line = re.search(r"^forge_source\\s*:\\s*(.+?)\\s*$", fm, re.MULTILINE)
        if source_line:
            forge_source = source_line.group(1).strip()
        np_line = re.search(r"^never_publish\\s*:\\s*(true|True|1)\\s*$", fm, re.MULTILINE)
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
'''


def main() -> int:
    forge_dir = Path("forge")
    watch_path = forge_dir / "watch.py"

    if not watch_path.exists():
        print("X forge/watch.py not found — run from ~/code/skill-forge")
        return 1

    s = watch_path.read_text()

    # Detect: has the file been stage1-patched (has _sync_secondary_stack_symlinks)?
    if "_sync_secondary_stack_symlinks" not in s:
        print("X watch.py doesn't have the stage 1 nesting patch — run stage1 first")
        return 1

    if "_install_imported_skill" in s:
        print("  + import handling already wired (skipping)")
    else:
        # Find install_skill and its helper _sync_secondary_stack_symlinks; replace both
        start_re = re.compile(r"^def install_skill\b", re.MULTILINE)
        m_start = start_re.search(s)
        if not m_start:
            print("X couldn't find install_skill in watch.py")
            return 1

        # Find the end: the def right after _sync_secondary_stack_symlinks
        # (that's the helper from stage 1; we need to replace both functions)
        helper_re = re.compile(r"^def _sync_secondary_stack_symlinks\b", re.MULTILINE)
        helper_m = helper_re.search(s, m_start.end())
        if not helper_m:
            print("X couldn't find _sync_secondary_stack_symlinks helper")
            return 1
        # End of _sync_secondary_stack_symlinks = next top-level def/class
        next_def_re = re.compile(r"^(def |class )", re.MULTILINE)
        end_m = next_def_re.search(s, helper_m.end())
        end = end_m.start() if end_m else len(s)

        s = s[:m_start.start()] + NEW_INSTALL_SKILL + "\n\n" + s[end:]
        watch_path.write_text(s)
        print("  + replaced install_skill + _sync_secondary_stack_symlinks (import-aware)")

    # Clear .pyc
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    # Parse-check
    try:
        ast.parse(watch_path.read_text())
        print("  + watch.py parses cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    # Ensure pending/ exists in the vault
    env_vars = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
    vault_path = env_vars.get("VAULT_PATH")
    if vault_path:
        pending = Path(vault_path).expanduser() / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        print(f"  + ensured pending/ exists at {pending}")

    print()
    print("+ stage 4 complete")
    print()
    print("How it works now:")
    print("  - Captured skills (drag from proposals/) → install SKILL.md only")
    print("  - Imported skills (drag from pending/)   → copy whole skill dir + LICENSE")
    print()
    print("Restart the watcher:")
    print("  pkill -9 -f 'forge watch'")
    print("  cd ~/code/skill-forge")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    print()
    print("Then approve one of the pending proposals:")
    print("  mv ~/Downloads/skill-forge/pending/netsuite-uif-spa-reference.md ~/Downloads/skill-forge/approved/")
    print("  tail -f ~/.skill-forge/watch.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
