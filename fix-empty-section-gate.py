#!/usr/bin/env python3
"""Fix the quality gate's empty-section regex.

Old pattern: r'^(##[^\\n]+)\\n+(?=##|\\Z)' — flags a section as empty if only
blank lines come before the next ## or end of file. Too aggressive: it
incorrectly flags sections whose immediate content is a `###` subheading,
a table, a code block, a list, or a block quote.

New behavior: a section is empty only if the next non-blank line is another
`##` heading or end of file. Any other line counts as content.

Run from inside ~/code/skill-forge:
    python3 fix-empty-section-gate.py
"""
import ast
import re
import sys
from pathlib import Path


def main() -> int:
    gates_path = Path("forge/gates.py")
    if not gates_path.exists():
        print("X forge/gates.py not found — run from ~/code/skill-forge")
        return 1

    s = gates_path.read_text()

    # Check if already patched
    if "_section_is_truly_empty" in s:
        print("  + gate already patched (skipping)")
        return 0

    # ---- Replace the empty section regex constant and the gate_quality logic ----
    # Old shape:
    #   _EMPTY_SECTION_RE = re.compile(
    #       r"^(##[^\n]+)\n+(?=##|\Z)", re.MULTILINE
    #   )
    #
    # New behavior: a section is "truly empty" if the next non-blank, non-frontmatter
    # line after the heading is another `##` heading or EOF. Anything else counts.

    old_regex_block = """_EMPTY_SECTION_RE = re.compile(
    r"^(##[^\\n]+)\\n+(?=##|\\Z)", re.MULTILINE
)"""

    # Slightly different shape (some patches may have it as one line)
    old_regex_oneline = '_EMPTY_SECTION_RE = re.compile(r"^(##[^\\n]+)\\n+(?=##|\\Z)", re.MULTILINE)'

    new_helper = '''_SECTION_HEADING_RE = re.compile(r"^##[ \\t]+(.+?)[ \\t]*$", re.MULTILINE)


def _section_is_truly_empty(body: str, heading_match) -> bool:
    """A section is truly empty if the next non-blank line after the heading
    is another `##` heading (same level) or end-of-body.

    Subheadings (`###`+), tables, code fences, lists, quotes, paragraphs,
    raw HTML — all count as content.
    """
    # Body slice that comes after this heading's line
    after_heading = body[heading_match.end():]
    for raw_line in after_heading.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue  # blank line — keep looking
        # Found a non-blank line. Is it another `##` heading?
        if re.match(r"^##[ \\t]+", line):
            return True
        # Anything else (###, text, |, ```, -, >, etc) is content
        return False
    # Reached end-of-body with only blank lines after the heading
    return True


def _find_empty_section_headings(body: str) -> list[str]:
    """Return the heading text of any truly-empty ## sections."""
    out = []
    for m in _SECTION_HEADING_RE.finditer(body):
        if _section_is_truly_empty(body, m):
            out.append(m.group(1).strip())
    return out'''

    if old_regex_block in s:
        s = s.replace(old_regex_block, new_helper, 1)
        print("  + replaced _EMPTY_SECTION_RE (multiline form)")
    elif old_regex_oneline in s:
        s = s.replace(old_regex_oneline, new_helper, 1)
        print("  + replaced _EMPTY_SECTION_RE (single-line form)")
    else:
        print("X couldn't find the old _EMPTY_SECTION_RE pattern in gates.py")
        # Print the first match found, if any, so we can debug
        if "_EMPTY_SECTION_RE" in s:
            idx = s.find("_EMPTY_SECTION_RE")
            print("    (found _EMPTY_SECTION_RE but didn't match expected shape)")
            print("    current shape:")
            print("   ", s[idx:idx + 200].replace("\n", "\n    "))
        return 1

    # ---- Update gate_quality to use the new helper ----
    old_check = """    empty_sections = _EMPTY_SECTION_RE.findall(body)
    if empty_sections:
        names = ", ".join(s.strip().lstrip("#").strip() for s in empty_sections)
        findings.append(f"{len(empty_sections)} empty section(s): {names}")
        suggestions.append("add content or remove empty headers")"""

    new_check = """    empty_sections = _find_empty_section_headings(body)
    if empty_sections:
        names = ", ".join(empty_sections)
        findings.append(f"{len(empty_sections)} empty section(s): {names}")
        suggestions.append("add content or remove empty headers")"""

    if old_check in s:
        s = s.replace(old_check, new_check, 1)
        print("  + updated gate_quality to use new helper")
    else:
        print("  ! couldn't find the empty_sections check in gate_quality")
        print("    (the helper is installed but you may need to manually update the call site)")

    gates_path.write_text(s)

    # Parse-check
    try:
        ast.parse(s)
        print("  + gates.py parses cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    # Clear .pyc
    pycache = Path("forge/__pycache__")
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    print()
    print("+ done")
    print()
    print("To retry the Oracle skill import:")
    print("  mv ~/Downloads/skill-forge/rejected/netsuite-uif-spa-reference.md ~/Downloads/skill-forge/approved/")
    print("  rm ~/Downloads/skill-forge/rejected/netsuite-uif-spa-reference.gate-report.md")
    print()
    print("(Restart the watcher if it doesn't pick up the new code:")
    print("  pkill -9 -f 'forge watch' && cd ~/code/skill-forge && nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
