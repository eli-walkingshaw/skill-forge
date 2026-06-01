#!/usr/bin/env python3
"""Add inline #stack-name tags to SKILL.md bodies so Obsidian's tag pane
shows stack membership at a glance.

Changes:
  - Adds `inject_stack_tags(skill_path, stacks)` helper in forge/stacks.py
  - Modifies `set_skill_stacks()` to call it automatically on assignment
  - Adds `forge stack sync-tags` subcommand for one-shot regeneration
  - Backfills all existing skills during install (so current assignments
    immediately show up in Obsidian)

Tags are flat (#engineering, #data, #operations) added on a single line
right after the frontmatter. Existing tags on that line are preserved;
stack tags are kept in sync with the `stacks:` frontmatter field.

Run from inside ~/code/skill-forge:
    python3 add-stack-obsidian-tags.py
"""
import ast
import re
import sys
from pathlib import Path


# The helper we're injecting into stacks.py.
INJECT_HELPER = '''

# ---------- Inline Obsidian tags ---------------------------------------------

# Tags we manage are flat (#engineering, #data, etc). To find/remove them
# safely we mark them on a single line right after the frontmatter.
_STACK_TAGS_LINE_RE = re.compile(
    r"^(<!-- forge-stack-tags -->\\n)(.*)$",
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
    m = re.match(r"^(---\\s*\\n.*?\\n---\\s*\\n)", text, re.DOTALL)
    if not m:
        # No frontmatter — don't touch the file
        return False
    fm_end = m.end()
    before = text[:fm_end]
    after = text[fm_end:]

    desired_line = ""
    if stacks:
        tags = " ".join(f"#{s.strip()}" for s in stacks if s.strip())
        # The frontmatter regex consumes the trailing \\n of the close `---`, so
        # `after` starts at the line AFTER the closer. No leading \\n needed.
        # Shape: marker + tags + blank line.
        desired_line = f"<!-- forge-stack-tags -->\\n{tags}\\n\\n"

    # Find existing marker line (if any) at the very start of `after`.
    marker_re = re.compile(
        r"^<!-- forge-stack-tags -->\\n[^\\n]*\\n\\n",
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
'''


# Updated set_skill_stacks (calls inject_stack_tags after writing frontmatter).
NEW_SET_FN = '''def set_skill_stacks(skill_md_path: Path, stacks: list[str]) -> bool:
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

    new_text = "---\\n" + "\\n".join(out_lines) + "\\n---\\n" + body_text

    frontmatter_changed = new_text != text
    if frontmatter_changed:
        skill_md_path.write_text(new_text, encoding="utf-8")

    # Now sync the inline #stack tags. inject_stack_tags handles add/update/remove.
    tags_changed = inject_stack_tags(skill_md_path, stacks)

    return frontmatter_changed or tags_changed
'''


# New subcommand: forge stack sync-tags
SYNC_TAGS_BLOCK = '''
    if sub == "sync-tags":
        from .stacks import list_skill_assignments, inject_stack_tags
        assignments = list_skill_assignments(config)
        changed = 0
        for skill_md, stacks in assignments.items():
            # Skip never_publish — no stack membership
            if stacks == ["(never_publish)"]:
                stacks = []
            if inject_stack_tags(skill_md, stacks):
                changed += 1
                print(f"  + {skill_md.parent.name}: {stacks or '(removed tags)'}")
        print(f"\\nsynced {changed} skill(s)")
        return 0
'''


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "stacks.py").exists():
        print("X forge/stacks.py not found — run install-stacks.py first")
        return 1

    # ---- Step 1: add inject_stack_tags to stacks.py ----
    stacks_path = forge_dir / "stacks.py"
    s = stacks_path.read_text()

    if "def inject_stack_tags" in s:
        print("  + inject_stack_tags already in stacks.py")
    else:
        # Append at the end
        s = s.rstrip() + "\n" + INJECT_HELPER
        print("  + added inject_stack_tags() to stacks.py")

    # ---- Step 2: replace set_skill_stacks ----
    set_fn_start = re.compile(r"^def set_skill_stacks\b", re.MULTILINE)
    m_start = set_fn_start.search(s)
    if not m_start:
        print("X couldn't find set_skill_stacks in stacks.py")
        return 1
    next_top = re.compile(r"^(def |class |# ----)", re.MULTILINE).search(s, m_start.end())
    end = next_top.start() if next_top else len(s)
    s = s[:m_start.start()] + NEW_SET_FN + "\n\n" + s[end:]
    print("  + replaced set_skill_stacks (now calls inject_stack_tags)")

    stacks_path.write_text(s)

    # ---- Step 3: add sync-tags subcommand to cmd_stack in commands.py ----
    cmds_path = forge_dir / "commands.py"
    cmds = cmds_path.read_text()

    if "if sub == \"sync-tags\":" in cmds:
        print("  + sync-tags already in cmd_stack")
    else:
        # Insert before the final `print(f"unknown stack subcommand: ...`
        anchor = '    print(f"unknown stack subcommand: {sub}")'
        if anchor not in cmds:
            print("X couldn't find cmd_stack dispatcher tail")
            return 1
        cmds = cmds.replace(anchor, SYNC_TAGS_BLOCK + "\n" + anchor, 1)
        cmds_path.write_text(cmds)
        print("  + added sync-tags branch to cmd_stack")

    # ---- Step 4: add the subparser ----
    main_path = forge_dir / "__main__.py"
    main_src = main_path.read_text()
    if 'stack_sub.add_parser("sync-tags"' not in main_src:
        # Insert after the `assign` subparser
        anchor = 'stack_sub.add_parser("assign", help="Interactive: assign existing skills to stacks")'
        if anchor in main_src:
            insertion = anchor + '\n    stack_sub.add_parser("sync-tags", help="Regenerate inline #stack tags from frontmatter")'
            main_src = main_src.replace(anchor, insertion, 1)
            main_path.write_text(main_src)
            print("  + added sync-tags subparser to __main__.py")
    else:
        print("  + sync-tags subparser already wired")

    # ---- Parse-check ----
    try:
        ast.parse(stacks_path.read_text())
        ast.parse(cmds_path.read_text())
        ast.parse(main_path.read_text())
        print("  + all files parse cleanly")
    except SyntaxError as e:
        print(f"X syntax error after patch: {e}")
        return 1

    # Clear .pyc cache
    pycache = Path("forge/__pycache__")
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()

    # ---- Step 5: backfill (do the work that justifies running this script) ----
    print()
    print("Backfilling inline tags from existing frontmatter...")

    # Run the new sync-tags command inline (don't shell out — keep it simple)
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "forge", "stack", "sync-tags"],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"X sync-tags failed: {result.stderr}")
        return 1

    print()
    print("+ done")
    print()
    print("In Obsidian, hit Cmd+R. The tag pane should now show #engineering,")
    print("#data, #operations alongside your existing topic tags.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
