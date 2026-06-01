#!/usr/bin/env python3
"""Wire _rewrite_wrapper_tags into the watcher's archive and reject paths.

The previous patch (add-pending-tag-lifecycle.py) installed the
`_rewrite_wrapper_tags` helper but couldn't auto-detect where to call it.
Now that we've seen the actual shape of watch.py, this patch wires it
correctly:

  - In the archive path (after install succeeds): rewrite frontmatter tags
    to [installed] right before the archive move
  - In the reject path (after gates fail): rewrite frontmatter tags to
    [rejected, gate-failed] right before the rejected move

The existing `inject_tags_into_callout` calls handle the callout-visible
tags (#archived #installed in the markdown). This patch adds the
complementary frontmatter tag rewrite so Obsidian's tag pane reflects
the actual state.

Run from inside ~/code/skill-forge:
    python3 wire-wrapper-tag-rewrite.py
"""
import ast
import re
import sys
from pathlib import Path


def main() -> int:
    watch_path = Path("forge/watch.py")
    if not watch_path.exists():
        print("X forge/watch.py not found — run from ~/code/skill-forge")
        return 1

    s = watch_path.read_text()

    # Sanity checks: the prerequisites should be in place
    if "def _rewrite_wrapper_tags" not in s:
        print("X _rewrite_wrapper_tags helper not in watch.py")
        print("  Run add-pending-tag-lifecycle.py first.")
        return 1

    # Check if already wired
    archive_wired = "_rewrite_wrapper_tags(path, ['installed'])" in s
    reject_wired = "_rewrite_wrapper_tags(path, ['rejected', 'gate-failed'])" in s

    if archive_wired and reject_wired:
        print("  + both archive and reject paths already wired")
        return 0

    # ---- Archive path ----
    # Locate the existing inject_tags_into_callout([#archived, #installed]) call.

    if archive_wired:
        print("  + archive path already wired")
    else:
        # Use a simpler regex that captures the try block; we'll insert
        # _rewrite_wrapper_tags before the try (at the same indent).
        archive_re = re.compile(
            r'^([ \t]*)try:\n'
            r'\s+tagged = inject_tags_into_callout\(\s*\n'
            r'\s+path\.read_text\([^)]*\),\s*\n'
            r'\s+\["#archived", "#installed"\],\s*\n'
            r'\s+\)\s*\n'
            r'\s+path\.write_text\(tagged[^)]*\)\s*\n'
            r'\s+except OSError:\s*\n'
            r'\s+pass',
            re.MULTILINE,
        )
        m = archive_re.search(s)
        if not m:
            print("  X couldn't find archive inject_tags_into_callout block")
            print("    Manual edit: find the inject_tags_into_callout([#archived, #installed]) try/except")
            print("    Add `_rewrite_wrapper_tags(path, ['installed'])` right before or after it.")
            return 1
        indent = m.group(1)
        insertion = f"{indent}_rewrite_wrapper_tags(path, ['installed'])\n"
        s = s[: m.start()] + insertion + s[m.start():]
        print("  + wired archive path")

    # ---- Reject path ----

    if reject_wired:
        print("  + reject path already wired")
    else:
        reject_re = re.compile(
            r'^([ \t]*)try:\n'
            r'\s+tagged = inject_tags_into_callout\(\s*\n'
            r'\s+path\.read_text\([^)]*\),\s*\n'
            r'\s+\["#rejected", "#gate-failed"\],\s*\n'
            r'\s+\)\s*\n'
            r'\s+path\.write_text\(tagged[^)]*\)\s*\n'
            r'\s+except OSError:\s*\n'
            r'\s+pass',
            re.MULTILINE,
        )
        m = reject_re.search(s)
        if not m:
            print("  X couldn't find reject inject_tags_into_callout block")
            print("    Manual edit: find the inject_tags_into_callout([#rejected, #gate-failed]) try/except")
            print("    Add `_rewrite_wrapper_tags(path, ['rejected', 'gate-failed'])` right before or after it.")
            return 1
        indent = m.group(1)
        insertion = f"{indent}_rewrite_wrapper_tags(path, ['rejected', 'gate-failed'])\n"
        s = s[: m.start()] + insertion + s[m.start():]
        print("  + wired reject path")

    watch_path.write_text(s)

    # Parse-check
    try:
        ast.parse(s)
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

    print()
    print("+ done")
    print()
    print("Restart the watcher to load the new behavior:")
    print("  pkill -9 -f 'forge watch'")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    print()
    print("(Existing files in archive/ and rejected/ will keep their old tags.")
    print(" To backfill those, see backfill section below.)")

    # ---- Backfill ----
    print()
    print("Backfilling existing archive/ and rejected/ files...")

    env_vars = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
    vault_path_str = env_vars.get("VAULT_PATH")
    if not vault_path_str:
        print("  ! VAULT_PATH not in .env — skipping backfill")
        return 0
    vault = Path(vault_path_str).expanduser()

    # We need to import _rewrite_wrapper_tags from the now-patched watch.py
    sys.path.insert(0, ".")
    try:
        from forge.watch import _rewrite_wrapper_tags
    except ImportError as e:
        print(f"  ! couldn't import _rewrite_wrapper_tags: {e}")
        return 0

    archive_count = 0
    archive_dir = vault / "archive"
    if archive_dir.exists():
        for f in archive_dir.iterdir():
            if f.suffix == ".md" and f.name.startswith("installed__"):
                _rewrite_wrapper_tags(f, ["installed"])
                archive_count += 1
        print(f"  + retagged {archive_count} archived file(s) → tags: [installed]")

    rejected_count = 0
    rejected_dir = vault / "rejected"
    if rejected_dir.exists():
        for f in rejected_dir.iterdir():
            if f.suffix == ".md" and not f.name.endswith(".gate-report.md"):
                _rewrite_wrapper_tags(f, ["rejected", "gate-failed"])
                rejected_count += 1
        print(f"  + retagged {rejected_count} rejected file(s) → tags: [rejected, gate-failed]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
