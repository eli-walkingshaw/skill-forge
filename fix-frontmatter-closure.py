#!/usr/bin/env python3
"""Fix the capture frontmatter-not-closed bug.

The thin-draft model sometimes produces SKILL.md without a closing '---' before
the body, breaking watcher validation. Two fixes:

1. Strengthen the system prompt to demand frontmatter closure.
2. Add an auto-repair: if the produced skill_md is missing the closing
   frontmatter delimiter before the first heading, insert it.

Run from inside ~/code/skill-forge:
    python3 fix-frontmatter-closure.py
"""
import re
import sys
from pathlib import Path


def main() -> int:
    p = Path("forge/commands.py")
    if not p.exists():
        print("✗ forge/commands.py not found — run from ~/code/skill-forge")
        return 1

    s = p.read_text()
    changes = 0

    # --- Change 1: strengthen prompt ---
    old_rule = (
        '- The USED_CONTEXT line must come first, then DRAFT_QUALITY on the next line, '
        'then a blank line, then the SKILL.md frontmatter. No preamble."""'
    )
    new_rule = (
        '- The USED_CONTEXT line must come first, then DRAFT_QUALITY on the next line, '
        'then a blank line, then the SKILL.md frontmatter. No preamble.\n'
        '- CRITICAL: the SKILL.md frontmatter MUST be closed with a line containing exactly `---` '
        'before the first `# Title` heading. The shape is `---\\nname: x\\ndescription: y\\n---` — '
        'three dashes open, three dashes close. Skipping the closing `---` breaks downstream parsing."""'
    )

    if old_rule in s:
        s = s.replace(old_rule, new_rule)
        print("  ✓ strengthened prompt to demand frontmatter closure")
        changes += 1
    elif "CRITICAL: the SKILL.md frontmatter MUST be closed" in s:
        print("  ✓ prompt already strengthened (skipping)")
    else:
        print("  ! couldn't find the prompt rule to strengthen (continuing)")

    # --- Change 2: add auto-repair before validation ---
    # We insert a call to _ensure_frontmatter_closed() right after
    # _strip_header_lines() and before SKILL_NAME_RE.search().
    old_block = """    skill_md = strip_outer_fence(raw)

    # The model prefixes output with USED_CONTEXT and DRAFT_QUALITY lines.
    # Pull both, then strip from the skill content.
    used_context = _parse_used_context(skill_md)
    draft_quality = _parse_draft_quality(skill_md)
    skill_md = _strip_header_lines(skill_md)

    m = SKILL_NAME_RE.search(skill_md)"""

    new_block = """    skill_md = strip_outer_fence(raw)

    # The model prefixes output with USED_CONTEXT and DRAFT_QUALITY lines.
    # Pull both, then strip from the skill content.
    used_context = _parse_used_context(skill_md)
    draft_quality = _parse_draft_quality(skill_md)
    skill_md = _strip_header_lines(skill_md)

    # Defensive: if the model produced unclosed frontmatter, insert the
    # closing '---' before the first heading so the watcher can validate.
    skill_md = _ensure_frontmatter_closed(skill_md)

    m = SKILL_NAME_RE.search(skill_md)"""

    if old_block in s:
        s = s.replace(old_block, new_block)
        print("  ✓ wired _ensure_frontmatter_closed() into cmd_capture")
        changes += 1
    elif "_ensure_frontmatter_closed(skill_md)" in s:
        print("  ✓ _ensure_frontmatter_closed() call already wired")
    else:
        print("  ✗ couldn't find the cmd_capture block to patch")
        return 1

    # --- Change 3: add the helper function ---
    helper = '''

def _ensure_frontmatter_closed(skill_md: str) -> str:
    """If the SKILL.md frontmatter is missing its closing '---', insert one.

    Looks for opening '---' at the start, then a closing '---' before the
    first heading or before EOF. If absent, inserts '---' right before the
    first heading line (or at end of file).
    """
    text = skill_md.lstrip()
    if not text.startswith("---"):
        return skill_md  # No frontmatter at all — leave it for validation to catch.

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return skill_md

    # Find a closing '---' between line 1 and the first heading line.
    heading_idx = None
    closing_idx = None
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            closing_idx = i
            break
        if line.startswith("#"):
            heading_idx = i
            break

    if closing_idx is not None:
        return skill_md  # Already properly closed.

    # Frontmatter never closed. Insert '---' before the heading (or at end).
    insert_at = heading_idx if heading_idx is not None else len(lines)
    # Insert a blank line + --- + blank line before the heading for clean separation.
    new_lines = lines[:insert_at] + ["---", ""] + lines[insert_at:]
    return "\\n".join(new_lines) + ("\\n" if skill_md.endswith("\\n") else "")


'''

    if "_ensure_frontmatter_closed" not in s.replace(
        "_ensure_frontmatter_closed(skill_md)", ""  # ignore the call site
    ):
        # Function not defined. Insert before the watch command at end of file.
        # Look for a stable anchor: the _parse_draft_quality function.
        marker = "def _parse_draft_quality"
        idx = s.find(marker)
        if idx == -1:
            print("  ✗ couldn't find _parse_draft_quality to anchor insertion")
            return 1
        s = s[:idx] + helper.lstrip() + "\n\n" + s[idx:]
        print("  ✓ added _ensure_frontmatter_closed() helper")
        changes += 1
    else:
        print("  ✓ _ensure_frontmatter_closed() helper already exists")

    p.write_text(s)
    print()
    print(f"✓ patched commands.py ({changes} changes)")

    # Sanity check
    print()
    print("Verification:")
    helper_count = sum(1 for _ in re.finditer(r"^def _ensure_frontmatter_closed\b", s, re.MULTILINE))
    call_count = s.count("_ensure_frontmatter_closed(skill_md)")
    print(f"  helper definitions: {helper_count} (want 1)")
    print(f"  call sites:         {call_count} (want 1)")
    print(f"  prompt strengthened: {'CRITICAL: the SKILL.md frontmatter' in s}")

    if helper_count != 1 or call_count != 1:
        print("  ✗ unexpected count — check commands.py manually")
        return 1

    print()
    print("Now retry:")
    print('  python3 -m forge capture --note "test the autolink loop fires"')
    print("  mv ~/Downloads/skill-forge/proposals/*.md ~/Downloads/skill-forge/approved/")
    print("  tail -f ~/.skill-forge/watch.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
