#!/usr/bin/env python3
"""Remove pending/ folder code from forge (Model A migration).

Model A uses a long-lived `pending` BRANCH instead of a `pending/` FOLDER.
The `forge pending` subcommand and its helpers are no longer needed —
contributors interact with git directly.

This patch is purely subtractive:
  - Removes `cmd_pending` and `_pending_list/view/reject/promote` from commands.py
  - Removes `pending` subparser from __main__.py
  - Removes the `cmd_pending` import from __main__.py

What stays:
  - BLESSED_STACKS / _is_blessed_stack helper (still useful for dotfile/non-stack exclusion)
  - cmd_gates / `forge gates` subcommand (agnostic to branch model)
  - read_skill_frontmatter helper (used elsewhere)

Idempotent: re-running detects what's already removed and skips.

Run from inside ~/code/skill-forge:
    python3 remove-pending-cli.py
"""
import ast
import re
import sys
from pathlib import Path


def remove_cmd_pending_block(commands_py: Path) -> bool:
    """Remove the cmd_pending function and its helpers from commands.py.

    The block to remove spans from the `# ---- forge pending ----` header
    (added by install-pending-cli.py / install-pending-promote.py) down
    through the last helper function (_pending_promote in Model B).
    """
    s = commands_py.read_text()

    # The pending block is bracketed by our header comment from install-pending-cli.py
    header_pattern = "# ---------- forge pending"

    if header_pattern not in s:
        print("  + cmd_pending block already removed from commands.py")
        return False

    # Find the start of the block (the header)
    header_pos = s.find(header_pattern)
    # Walk back to the preceding blank-line boundary so we don't leave dangling whitespace
    block_start = header_pos
    while block_start > 0 and s[block_start - 1] != "\n":
        block_start -= 1

    # Find the END of the block: scan forward for the next `# ----` header,
    # or end of file. The _pending_promote function is the last one added.
    # We look for either:
    #   1. Another `# ----` header (means there's another section after)
    #   2. End of file
    after_header_pos = header_pos + len(header_pattern)
    next_header_match = re.search(r"\n# ----+ forge ", s[after_header_pos:])
    if next_header_match:
        block_end = after_header_pos + next_header_match.start() + 1  # +1 to include the trailing newline
    else:
        block_end = len(s)

    # Sanity check: the block we're removing should contain `def cmd_pending`
    block_text = s[block_start:block_end]
    if "def cmd_pending(" not in block_text:
        print("  ! found pending header but no cmd_pending function in block — aborting")
        print(f"  (block was {len(block_text)} chars; first 200: {block_text[:200]})")
        return False

    new_s = s[:block_start] + s[block_end:]
    # Collapse trailing whitespace before EOF
    new_s = new_s.rstrip() + "\n"
    commands_py.write_text(new_s)
    print(f"  + removed cmd_pending block ({len(block_text)} chars) from commands.py")
    return True


def remove_pending_import(main_py: Path) -> bool:
    """Remove `cmd_pending` from the imports in __main__.py."""
    s = main_py.read_text()
    if "cmd_pending" not in s:
        print("  + cmd_pending import already removed from __main__.py")
        return False

    lines = s.splitlines(keepends=True)
    new_lines = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        # The line will look like `    cmd_pending,` inside the import block
        if stripped == "cmd_pending,":
            removed += 1
            continue
        new_lines.append(line)
    if removed == 0:
        print("  ! cmd_pending appears in __main__.py but not in expected format")
        return False
    main_py.write_text("".join(new_lines))
    print(f"  + removed cmd_pending import line from __main__.py")
    return True


def remove_pending_subparser(main_py: Path) -> bool:
    """Remove the pending subparser block from __main__.py."""
    s = main_py.read_text()
    if 'pending_p = sub.add_parser("pending"' not in s:
        print("  + pending subparser already removed from __main__.py")
        return False

    lines = s.splitlines(keepends=True)
    # Find the pending subparser block. Starts with `    pending_p = sub.add_parser(...)`
    # and ends with `    pending_p.set_defaults(fn=cmd_pending)`
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if 'pending_p = sub.add_parser("pending"' in line:
            start_idx = i
        if start_idx is not None and "pending_p.set_defaults(fn=cmd_pending)" in line:
            end_idx = i
            break

    if start_idx is None or end_idx is None:
        print("  ! couldn't bracket the pending subparser block (start={}, end={})".format(
            start_idx, end_idx))
        return False

    # Also strip any leading blank line just before the block
    while start_idx > 0 and lines[start_idx - 1].strip() == "":
        start_idx -= 1

    new_lines = lines[:start_idx] + lines[end_idx + 1:]
    main_py.write_text("".join(new_lines))
    print(f"  + removed pending subparser block (lines {start_idx+1}-{end_idx+1}) from __main__.py")
    return True


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge", file=sys.stderr)
        return 1

    commands_path = forge_dir / "commands.py"
    main_path = forge_dir / "__main__.py"

    remove_cmd_pending_block(commands_path)
    remove_pending_import(main_path)
    remove_pending_subparser(main_path)

    # Parse-check
    for p in [commands_path, main_path]:
        try:
            ast.parse(p.read_text())
        except SyntaxError as e:
            print(f"X syntax error in {p}: {e}", file=sys.stderr)
            return 1
    print("  + all files parse cleanly")

    # Clear pyc cache
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()

    print()
    print("+ done")
    print()
    print("Verify:")
    print("  python3 -m forge --help              # should NOT show `pending` subcommand")
    print("  python3 -m forge stack list          # should still work")
    print("  python3 -m forge gates <some.md>     # should still work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
