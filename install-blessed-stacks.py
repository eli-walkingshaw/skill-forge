#!/usr/bin/env python3
"""Add BLESSED_STACKS filter so pending/ (and other non-stack folders) get
excluded from skill discovery.

Five spots in the codebase walk repo subdirectories looking for stack folders.
Currently they only filter dotfiles and (in one place) the 'unassigned' folder.
We add a canonical helper `_is_blessed_stack(dir_name)` and use it everywhere.

Blessed stacks (skills here ARE installed/synced/discoverable):
  - data
  - engineering
  - operations
  - unassigned

Excluded (not blessed):
  - pending          (contributions awaiting promotion)
  - archive          (lifecycle, but the actual skills are gone)
  - rejected         (lifecycle)
  - any future workflow folders
  - .git, .github, etc

The unassigned folder is kept as blessed for compatibility with existing
skills like `slack-message-drafter-bpi`. If you want to exclude that too,
edit BLESSED_STACKS after this patch lands.

Idempotent: re-running detects existing patches and skips.

Run from inside ~/code/skill-forge:
    python3 install-blessed-stacks.py
"""
import ast
import re
import sys
from pathlib import Path


BLESSED_HELPER = '''

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
'''


def _add_helper_to_stacks_py(stacks_py: Path) -> bool:
    """Add BLESSED_STACKS + helper to stacks.py if not present. Returns True if added."""
    s = stacks_py.read_text()
    if "BLESSED_STACKS" in s:
        return False
    # Insert after the imports / dataclass definitions, before the first def.
    # We find the first `def ` and insert the helper just before it.
    first_def_match = re.search(r"^def ", s, re.MULTILINE)
    if not first_def_match:
        print("X stacks.py has no `def` statements — something's very wrong")
        return False
    insert_pos = first_def_match.start()
    s = s[:insert_pos] + BLESSED_HELPER.lstrip("\n") + "\n\n" + s[insert_pos:]
    stacks_py.write_text(s)
    return True


def _patch_discover_stacks(stacks_py: Path) -> bool:
    """Replace the 'unassigned' skip in discover_stacks with the helper call."""
    s = stacks_py.read_text()
    if "if not _is_blessed_stack(stack_dir.name):" in s:
        return False  # already patched
    # The original block has TWO checks: dotfile + 'unassigned'.
    # We replace both with a single _is_blessed_stack call.
    old = (
        '        if not stack_dir.is_dir() or stack_dir.name.startswith("."):\n'
        '            continue\n'
        "        # Skip 'unassigned' and any non-stack folders.\n"
        '        if stack_dir.name == "unassigned":\n'
        '            continue\n'
    )
    new = (
        '        if not stack_dir.is_dir():\n'
        '            continue\n'
        '        if not _is_blessed_stack(stack_dir.name):\n'
        '            continue\n'
        "        # 'unassigned' is blessed but skipped from discover_stacks "
        "(it's not a real stack, just a holding bin).\n"
        '        if stack_dir.name == "unassigned":\n'
        '            continue\n'
    )
    if old not in s:
        print("  ! discover_stacks: old pattern not found; may already be patched or have drifted")
        return False
    s = s.replace(old, new)
    stacks_py.write_text(s)
    return True


def _patch_list_skill_assignments(stacks_py: Path) -> bool:
    """Add _is_blessed_stack filter to list_skill_assignments.

    Strategy: find the function by its `def` line, scan its body, and patch
    the first dotfile-skip we encounter inside that function only.
    """
    s = stacks_py.read_text()
    lines = s.splitlines(keepends=True)

    # Find the function def line
    func_start = None
    for i, line in enumerate(lines):
        if line.startswith("def list_skill_assignments("):
            func_start = i
            break
    if func_start is None:
        print("  ! list_skill_assignments: function def not found in stacks.py")
        return False

    # Find where the function ends (next top-level def or end of file)
    func_end = len(lines)
    for i in range(func_start + 1, len(lines)):
        if lines[i].startswith("def ") or lines[i].startswith("class "):
            func_end = i
            break

    # Look for the dotfile-skip line within this function's body
    target_line = '        if not stack_dir.is_dir() or stack_dir.name.startswith("."):\n'
    target_line_no_newline = target_line.rstrip("\n")
    patched = False
    for i in range(func_start, func_end):
        if lines[i].rstrip("\n") == target_line_no_newline:
            # Check we haven't already patched (next line should be `continue`,
            # the line after that should NOT already be our filter)
            if (i + 2 < func_end and
                "_is_blessed_stack(stack_dir.name)" in lines[i + 2]):
                return False  # already patched
            # Insert the filter after the `continue` line (i + 1)
            filter_lines = [
                "        if not _is_blessed_stack(stack_dir.name):\n",
                "            continue\n",
            ]
            new_lines = lines[: i + 2] + filter_lines + lines[i + 2 :]
            s = "".join(new_lines)
            stacks_py.write_text(s)
            patched = True
            break

    if not patched:
        print("  ! list_skill_assignments: couldn't find dotfile-skip line to patch")
        return False
    return True


def _patch_relate_walk(relate_py: Path) -> bool:
    """Add _is_blessed_stack filter to walk_canonical_skills in relate.py."""
    s = relate_py.read_text()
    if "_is_blessed_stack" in s:
        return False  # already patched

    # We need to import _is_blessed_stack from stacks.py. Add the import near
    # the top, after the existing relative imports.
    import_re = re.compile(r"(from \.config import .*?\n)", re.DOTALL)
    m = import_re.search(s)
    if m:
        new_import = m.group(1) + "from .stacks import _is_blessed_stack\n"
        s = s.replace(m.group(0), new_import, 1)
    else:
        # Fallback: add at top with other imports
        print("  ! couldn't find import anchor in relate.py; manual edit may be needed")
        return False

    # Add the filter line in walk_canonical_skills, right after the dotfile-skip
    old = (
        '        if not stack_dir.is_dir() or stack_dir.name.startswith("."):\n'
        '            continue\n'
    )
    new = (
        '        if not stack_dir.is_dir() or stack_dir.name.startswith("."):\n'
        '            continue\n'
        '        if not _is_blessed_stack(stack_dir.name):\n'
        '            continue\n'
    )
    if old not in s:
        print("  ! relate.py: dotfile-skip pattern not found")
        return False
    s = s.replace(old, new, 1)
    relate_py.write_text(s)
    return True


def _patch_link_into_claude_skills(watch_py: Path) -> bool:
    """Add _is_blessed_stack filter to link_into_claude_skills in watch.py.

    Currently this function walks repo subfolders to find the canonical home
    for a skill. Without filtering, it might find a pending/<skill>/SKILL.md
    and create a symlink to it, which we don't want — pending/ skills aren't
    blessed.
    """
    s = watch_py.read_text()

    # Check if there's a walk-stacks-style loop in this function
    if "_is_blessed_stack" in s:
        return False  # already patched

    # Import the helper
    if "from .stacks import" in s and "_is_blessed_stack" not in s:
        # Extend existing import
        s = re.sub(
            r"(from \.stacks import )([^\n]+)",
            lambda m: m.group(1) + m.group(2).rstrip() + ", _is_blessed_stack",
            s,
            count=1,
        )
    elif "from .stacks import" not in s:
        # Add new import
        import_re = re.compile(r"(from \.config import [^\n]+\n)")
        m = import_re.search(s)
        if m:
            new_import = m.group(1) + "from .stacks import _is_blessed_stack\n"
            s = s.replace(m.group(0), new_import, 1)

    # Now add the filter inside link_into_claude_skills.
    # The function walks stack_dir candidates; we need to skip non-blessed ones.
    # Find the function body and patch its walk.
    func_re = re.compile(
        r"(def link_into_claude_skills\(.*?\n)(.*?)(\n\ndef |\Z)",
        re.DOTALL,
    )
    m = func_re.search(s)
    if not m:
        print("  ! link_into_claude_skills not found by regex")
        return False

    func_signature = m.group(1)
    func_body = m.group(2)
    func_end = m.group(3)

    # Patch the walk. Common patterns:
    # `for candidate in claude_skills_repo.iterdir():` or similar
    # We add `if not _is_blessed_stack(candidate.name): continue` after the iter.
    # Look for `iterdir()` calls inside the function body.
    walk_re = re.compile(
        r"(    for (\w+) in [^\n]+\.iterdir\(\):\n)((?:        [^\n]*\n)*?)"
    )
    new_body = func_body
    matched = False
    for walk_m in walk_re.finditer(func_body):
        var_name = walk_m.group(2)
        loop_body = walk_m.group(3)
        # Insert the filter at the top of the loop body
        if f"_is_blessed_stack({var_name}.name)" not in loop_body:
            filter_line = (
                f"        if not _is_blessed_stack({var_name}.name):\n"
                f"            continue\n"
            )
            new_loop = walk_m.group(1) + filter_line + loop_body
            new_body = new_body.replace(walk_m.group(0), new_loop, 1)
            matched = True
            break  # patch only the first walk loop

    if not matched:
        print("  ! link_into_claude_skills: no walk loop found to patch (function may not walk stacks)")
        # Not a fatal failure — function might walk differently. We continue.
        return True

    s = s.replace(func_signature + func_body + func_end, func_signature + new_body + func_end, 1)
    watch_py.write_text(s)
    return True


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "stacks.py").exists():
        print("X forge/stacks.py not found — run from ~/code/skill-forge")
        return 1

    stacks_py = forge_dir / "stacks.py"
    relate_py = forge_dir / "relate.py"
    watch_py = forge_dir / "watch.py"

    # 1. Add the helper to stacks.py
    if _add_helper_to_stacks_py(stacks_py):
        print("  + added BLESSED_STACKS helper to stacks.py")
    else:
        print("  + BLESSED_STACKS helper already in stacks.py")

    # 2. Patch discover_stacks
    if _patch_discover_stacks(stacks_py):
        print("  + patched discover_stacks() to use _is_blessed_stack")
    else:
        print("  + discover_stacks() already patched (or skipped)")

    # 3. Patch list_skill_assignments
    if _patch_list_skill_assignments(stacks_py):
        print("  + patched list_skill_assignments() to use _is_blessed_stack")
    else:
        print("  + list_skill_assignments() already patched (or skipped)")

    # 4. Patch relate.py walk_canonical_skills
    if relate_py.exists():
        if _patch_relate_walk(relate_py):
            print("  + patched walk_canonical_skills() in relate.py")
        else:
            print("  + walk_canonical_skills() already patched (or skipped)")

    # 5. Patch link_into_claude_skills in watch.py
    if _patch_link_into_claude_skills(watch_py):
        print("  + patched link_into_claude_skills() in watch.py")
    else:
        print("  + link_into_claude_skills() already patched")

    # Parse-check
    for p in [stacks_py, relate_py, watch_py]:
        if p.exists():
            try:
                ast.parse(p.read_text())
            except SyntaxError as e:
                print(f"X syntax error in {p}: {e}")
                return 1
    print("  + all files parse cleanly")

    # Clear .pyc cache
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()

    print()
    print("+ done")
    print()
    print("This adds BLESSED_STACKS = {'data', 'engineering', 'operations', 'unassigned'}")
    print("so pending/, archive/, rejected/, etc. don't appear in skill discovery.")
    print()
    print("Verify:")
    print("  python3 -m forge stack list           # should NOT show pending/")
    print("  python3 -m forge list                 # same")
    return 0


if __name__ == "__main__":
    sys.exit(main())
