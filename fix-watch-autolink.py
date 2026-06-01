#!/usr/bin/env python3
"""Patch forge/watch.py to auto-symlink installed skills into ~/.claude/skills/.

After a skill is installed + committed, this adds a symlink so Claude Code
picks it up without manual intervention.

Idempotent: safe to run multiple times. Adds a `link_into_claude_skills`
helper if missing, and inserts the call into `process_approved_file`.

Run from inside ~/code/skill-forge:
    python3 fix-watch-autolink.py
"""
import re
import sys
from pathlib import Path


HELPER_FN = '''
def link_into_claude_skills(config: Config, skill_name: str) -> None:
    """Create ~/.claude/skills/<skill_name> -> <repo>/<skill_name> if Claude Code's
    skills directory exists. Idempotent: skips if a correct symlink is already
    in place, warns and skips if a different file/symlink is in the way.
    """
    from pathlib import Path
    claude_skills = Path.home() / ".claude" / "skills"
    if not claude_skills.exists():
        # Claude Code isn't installed (or uses a different location); nothing to do.
        return
    link_path = claude_skills / skill_name
    target = config.skills_repo_path / skill_name
    if not target.exists():
        print(f"[watch] symlink skipped — target {target} doesn't exist")
        return
    if link_path.is_symlink():
        existing = link_path.resolve()
        if existing == target.resolve():
            print(f"[watch] symlink already correct: {link_path.name}")
            return
        print(f"[watch] symlink {link_path.name} points elsewhere — leaving it alone")
        return
    if link_path.exists():
        print(f"[watch] {link_path.name} exists and is not a symlink — leaving it alone")
        return
    try:
        link_path.symlink_to(target)
        print(f"[watch] linked → ~/.claude/skills/{skill_name}")
    except OSError as e:
        print(f"[watch] symlink failed: {e}")


'''

# The exact spot to insert the call in process_approved_file:
# right after the git commit/push block, before the archive move.
INSERT_AFTER = "        print(f\"[watch] committed{' + pushed' if config.git_auto_push else ''}\")\n    except subprocess.CalledProcessError as e:\n        print(f\"[watch] git operation failed: {e}\")\n        return\n"

INSERT_BLOCK = """
    # Symlink the new skill into ~/.claude/skills/ so Claude Code picks it up.
    link_into_claude_skills(config, name)

"""


def main() -> int:
    watch_py = Path("forge/watch.py")
    if not watch_py.exists():
        print("✗ forge/watch.py not found — run this from ~/code/skill-forge")
        return 1

    s = watch_py.read_text()

    # Step 1: add the helper function if it doesn't already exist.
    if "def link_into_claude_skills(" in s:
        print("  ✓ link_into_claude_skills() already defined")
    else:
        # Insert helper just before process_approved_file.
        marker = "def process_approved_file"
        idx = s.find(marker)
        if idx == -1:
            print("✗ couldn't find process_approved_file — bailing")
            return 1
        s = s[:idx] + HELPER_FN.lstrip() + "\n\n" + s[idx:]
        print("  ✓ added link_into_claude_skills()")

    # Step 2: insert the call into process_approved_file if not already there.
    call_marker = "link_into_claude_skills(config, name)"
    if call_marker in s:
        print("  ✓ link_into_claude_skills() call already in process_approved_file")
    else:
        if INSERT_AFTER not in s:
            print("✗ couldn't find the insertion point in process_approved_file")
            print("  (git commit/push block didn't match expected shape)")
            return 1
        s = s.replace(INSERT_AFTER, INSERT_AFTER + INSERT_BLOCK, 1)
        print("  ✓ wired call into process_approved_file")

    watch_py.write_text(s)
    print()
    print("✓ patched forge/watch.py")
    print()

    # Sanity check.
    helper_count = s.count("def link_into_claude_skills(")
    call_count = s.count("link_into_claude_skills(config, name)")
    print(f"  helper definitions:  {helper_count} (want 1)")
    print(f"  call sites:          {call_count} (want 1)")
    if helper_count != 1 or call_count != 1:
        print("  ✗ unexpected count — check forge/watch.py manually")
        return 1

    print()
    print("Restart the watcher to pick up the change:")
    print("  pkill -f 'forge watch' 2>/dev/null")
    print("  cd ~/code/skill-forge")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    print()
    print("Then approve a proposal and check that it auto-links:")
    print("  ls -la ~/.claude/skills/ | tail -3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
