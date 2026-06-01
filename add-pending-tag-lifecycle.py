#!/usr/bin/env python3
"""Add #pending → #installed/#rejected lifecycle tags via wrapper frontmatter.

Five things this patch does:

1. extract_skill_md (watch.py): teach it to handle proposals with wrapper
   frontmatter at the top, before the callout. The new shape is:

       ---
       tags: [pending]
       ---

       > [!info] proposal callout
       > ...

       ---             ← divider between wrapper and skill
       ---             ← SKILL.md frontmatter open
       name: ...
       ...
       ---             ← SKILL.md frontmatter close
       # Body

   When wrapper frontmatter exists, skip past it before looking for the
   divider. Old-style proposals (no wrapper frontmatter) still work.

2. sync.py: when writing a new pending/<name>.md, prefix the proposal with
   the wrapper frontmatter containing `tags: [pending]`.

3. Backfill the 6 existing pending/*.md files with wrapper frontmatter.

4. watch.py: on archival (install succeeded), rewrite the file's wrapper
   frontmatter to `tags: [installed]`. On rejection, rewrite to
   `tags: [rejected, gate-failed]`.

5. CSS snippet: add a color for #pending (orange — "needs attention").

Run from inside ~/code/skill-forge:
    python3 add-pending-tag-lifecycle.py
"""
import ast
import re
import sys
from pathlib import Path


# ---- 1. New extract_skill_md that handles wrapper frontmatter ----

NEW_EXTRACT_FN = '''def extract_skill_md(proposal_text: str) -> str | None:
    """A proposal note wraps the real SKILL.md after a divider. Strip the wrapper.

    Proposals can have an optional wrapper frontmatter block at the very top
    (used for lifecycle tags like `tags: [pending]`), then a callout, then a
    `---` divider, then the real SKILL.md.

    Structure:
        ---                    (optional wrapper frontmatter)
        tags: [pending]
        ---
        > [!info] ...
        > ...
        ---            <-- divider between wrapper and skill
        ---            <-- start of skill frontmatter
        name: ...
        ---
        # ...

    Tricky: we have to distinguish "this is a wrapped proposal" from "this is
    just a SKILL.md directly" (also starts with `---`). Wrapper frontmatter
    only contains lifecycle metadata (tags), not skill fields like name/description.
    If the first `---...---` block contains `name:`, it IS the skill, not wrapper.
    """
    text = proposal_text

    # Look at the first frontmatter block, if any
    fm_match = re.match(r"^---[ \\t]*\\n(.*?)\\n---[ \\t]*\\n", text, re.DOTALL)
    if fm_match:
        fm_body = fm_match.group(1)
        # If this frontmatter has name: / description:, it's the skill itself
        # (file is a bare SKILL.md, no wrapper). Don't consume it.
        is_skill_frontmatter = bool(
            re.search(r"^name\\s*:", fm_body, re.MULTILINE)
            or re.search(r"^description\\s*:", fm_body, re.MULTILINE)
        )
        if not is_skill_frontmatter:
            # It's wrapper frontmatter — skip past it
            text = text[fm_match.end():]

    # Now look for the divider between wrapper-callout and skill
    parts = text.split("\\n---\\n", 1)
    if len(parts) != 2:
        # No divider — maybe the user wrote the SKILL.md directly without wrapper
        if text.lstrip().startswith("---"):
            return text
        return None
    return parts[1].strip()
'''


# ---- 2. sync.py updates: add wrapper frontmatter at proposal creation ----

# The current sync code builds the proposal as callout_lines + "---" + annotated.
# We need to prefix wrapper frontmatter to that.

OLD_BUILD_HEADER = '''    callout_lines = [
        f"> [!info] skill-forge proposal (imported)",'''

NEW_BUILD_HEADER = '''    callout_lines = [
        f"---",
        f"tags: [pending]",
        f"---",
        f"",
        f"> [!info] skill-forge proposal (imported)",'''


# ---- 4. Watcher tag-rewriting helpers ----

WRAPPER_TAG_HELPER = '''

def _rewrite_wrapper_tags(path: Path, new_tags: list[str]) -> None:
    """Rewrite the wrapper-frontmatter `tags:` field of a proposal file.

    If the file has wrapper frontmatter at the very top, find the `tags:` line
    and replace its value. If no wrapper frontmatter, prepend one with the tags.
    Quietly no-ops if the file is missing or unreadable.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    tag_list = "[" + ", ".join(new_tags) + "]"

    fm_match = re.match(r"^(---[ \\t]*\\n)(.*?)(\\n---[ \\t]*\\n)", text, re.DOTALL)
    if fm_match:
        # Wrapper frontmatter exists — rewrite its `tags:` line, or add it
        fm_body = fm_match.group(2)
        if re.search(r"^tags\\s*:", fm_body, re.MULTILINE):
            new_body = re.sub(
                r"^tags\\s*:.*$",
                f"tags: {tag_list}",
                fm_body,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            new_body = fm_body.rstrip() + f"\\ntags: {tag_list}"
        new_text = fm_match.group(1) + new_body + fm_match.group(3) + text[fm_match.end():]
    else:
        # No wrapper frontmatter — prepend one
        new_text = f"---\\ntags: {tag_list}\\n---\\n\\n" + text

    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        pass
'''


# Find places in process_approved_file where the file is archived/rejected
# and patch them to call _rewrite_wrapper_tags first.


def patch_extract_skill_md(watch_src: str) -> tuple[str, bool]:
    """Replace extract_skill_md with the new version that handles wrapper frontmatter."""
    if "Tricky: we have to distinguish" in watch_src:
        return watch_src, False
    start_re = re.compile(r"^def extract_skill_md\b", re.MULTILINE)
    m = start_re.search(watch_src)
    if not m:
        return watch_src, False
    next_re = re.compile(r"^(def |class |[A-Z][A-Z_]+ = )", re.MULTILINE)
    next_m = next_re.search(watch_src, m.end())
    end = next_m.start() if next_m else len(watch_src)
    return watch_src[:m.start()] + NEW_EXTRACT_FN + "\n\n" + watch_src[end:], True


def add_wrapper_tag_helper(watch_src: str) -> tuple[str, bool]:
    """Add _rewrite_wrapper_tags helper after extract_skill_md."""
    if "def _rewrite_wrapper_tags(" in watch_src:
        return watch_src, False
    # Insert after extract_skill_md
    anchor_re = re.compile(r"^def extract_skill_md\b", re.MULTILINE)
    m = anchor_re.search(watch_src)
    if not m:
        return watch_src, False
    next_re = re.compile(r"^(def |class |[A-Z][A-Z_]+ = )", re.MULTILINE)
    next_m = next_re.search(watch_src, m.end())
    if not next_m:
        return watch_src + WRAPPER_TAG_HELPER, True
    insert_at = next_m.start()
    return watch_src[:insert_at] + WRAPPER_TAG_HELPER + watch_src[insert_at:], True


def patch_watcher_archive_call(watch_src: str) -> tuple[str, bool]:
    """Patch process_approved_file to rewrite tags before archive/reject moves.

    We look for the points where the file is moved to archive/ or rejected/
    and call _rewrite_wrapper_tags(path, ['installed']) or ['rejected', 'gate-failed']
    just before the move.

    The exact shape depends on the user's watcher version, so we use anchors
    that should be present.
    """
    if "_rewrite_wrapper_tags(path, " in watch_src:
        return watch_src, False

    changed = False

    # Pattern: archive after successful install
    # Looking for shutil.move(... path ..., archive_dir / ...) or similar
    archive_patterns = [
        # Common shape: archive_target = archive_dir / f"installed__{path.name}"; path.rename(archive_target)
        (
            r"(\s+)(archive_target = .*?\n\s+path\.rename\(archive_target\))",
            r"\1_rewrite_wrapper_tags(path, ['installed'])\n\1\2",
        ),
        # Alternative: shutil.move(str(path), str(archive_dir / ...))
        (
            r"(\s+)(shutil\.move\(str\(path\), str\(archive_dir / [^)]+\)\))",
            r"\1_rewrite_wrapper_tags(path, ['installed'])\n\1\2",
        ),
    ]
    for pat, repl in archive_patterns:
        new_src = re.sub(pat, repl, watch_src, count=1)
        if new_src != watch_src:
            watch_src = new_src
            changed = True
            break

    # Pattern: reject after gate failure
    reject_patterns = [
        (
            r"(\s+)(reject_target = .*?\n\s+path\.rename\(reject_target\))",
            r"\1_rewrite_wrapper_tags(path, ['rejected', 'gate-failed'])\n\1\2",
        ),
        (
            r"(\s+)(shutil\.move\(str\(path\), str\(rejected_dir / [^)]+\)\))",
            r"\1_rewrite_wrapper_tags(path, ['rejected', 'gate-failed'])\n\1\2",
        ),
    ]
    for pat, repl in reject_patterns:
        new_src = re.sub(pat, repl, watch_src, count=1)
        if new_src != watch_src:
            watch_src = new_src
            changed = True
            break

    return watch_src, changed


def patch_sync_proposal_header(sync_src: str) -> tuple[str, bool]:
    """Prepend wrapper frontmatter (tags: [pending]) to new proposals."""
    if "tags: [pending]" in sync_src and 'f"tags: [pending]"' in sync_src:
        return sync_src, False
    if OLD_BUILD_HEADER not in sync_src:
        return sync_src, False
    return sync_src.replace(OLD_BUILD_HEADER, NEW_BUILD_HEADER, 1), True


def backfill_pending_files(vault_path: Path) -> int:
    """Add wrapper frontmatter with tags: [pending] to all files in pending/."""
    pending_dir = vault_path / "pending"
    if not pending_dir.exists():
        return 0
    count = 0
    for f in sorted(pending_dir.iterdir()):
        if not f.is_file() or f.suffix != ".md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        # Skip if already has wrapper frontmatter
        if re.match(r"^---[ \\t]*\\n", text):
            continue
        # Prepend
        new_text = f"---\ntags: [pending]\n---\n\n" + text
        try:
            f.write_text(new_text, encoding="utf-8")
            count += 1
            print(f"  + tagged: {f.name}")
        except OSError as e:
            print(f"  - failed {f.name}: {e}")
    return count


def update_css_snippet(vault_path: Path) -> bool:
    """Add #pending color rule to the forge-stack-colors.css snippet."""
    css_path = vault_path / ".obsidian" / "snippets" / "forge-stack-colors.css"
    if not css_path.exists():
        return False
    s = css_path.read_text(encoding="utf-8")
    if '"#pending"' in s or "tag[href=\"#pending\"]" in s:
        return False
    rule = '''
/* #pending — orange, needs attention */
.tag[href="#pending"], a.tag[href="#pending"], .cm-hashtag[data-tag="pending"] {
  background-color: #f59e0b !important;
  color: white !important;
}
'''
    css_path.write_text(s.rstrip() + "\n" + rule, encoding="utf-8")
    return True


def main() -> int:
    forge_dir = Path("forge")
    watch_path = forge_dir / "watch.py"
    sync_path = forge_dir / "sync.py"
    if not watch_path.exists() or not sync_path.exists():
        print("X forge/watch.py or forge/sync.py missing — run from ~/code/skill-forge")
        print("  Make sure stages 1-4 have been applied first.")
        return 1

    # ---- Patch 1: extract_skill_md ----
    w = watch_path.read_text()
    w, changed = patch_extract_skill_md(w)
    if changed:
        print("  + patched extract_skill_md to skip wrapper frontmatter")
    else:
        print("  + extract_skill_md already wrapper-aware")

    # ---- Patch 2: add _rewrite_wrapper_tags helper ----
    w, changed = add_wrapper_tag_helper(w)
    if changed:
        print("  + added _rewrite_wrapper_tags helper")
    else:
        print("  + _rewrite_wrapper_tags already present")

    # ---- Patch 3: archive/reject calls ----
    if "_rewrite_wrapper_tags(path, " in w:
        print("  + archive/reject tag rewriting already wired")
    else:
        w, changed = patch_watcher_archive_call(w)
        if changed:
            print("  + patched archive/reject calls to rewrite tags")
        else:
            # Couldn't auto-detect; user will need manual integration
            print("  ! couldn't auto-wire archive/reject tag rewriting")
            print("    (the helper is installed; tags won't auto-update on archive)")

    watch_path.write_text(w)

    # ---- Patch 4: sync proposal header ----
    s = sync_path.read_text()
    s, changed = patch_sync_proposal_header(s)
    if changed:
        print("  + patched sync to prepend wrapper frontmatter on new proposals")
        sync_path.write_text(s)
    else:
        print("  + sync proposal header already has wrapper frontmatter")

    # ---- Parse-check ----
    try:
        ast.parse(watch_path.read_text())
        ast.parse(sync_path.read_text())
        print("  + watch.py and sync.py parse cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    # Clear .pyc
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    # ---- Load .env to find VAULT_PATH ----
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

    # ---- Backfill existing pending files ----
    print()
    print("Backfilling existing pending/ files...")
    n = backfill_pending_files(vault)
    print(f"  ({n} file(s) tagged)")

    # ---- Update CSS snippet ----
    print()
    print("Updating Obsidian CSS snippet...")
    if update_css_snippet(vault):
        print("  + added #pending color rule")
    else:
        print("  + #pending already in CSS snippet (or snippet missing)")

    print()
    print("+ done")
    print()
    print("Restart the watcher to load the patched code:")
    print("  pkill -9 -f 'forge watch'")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    return 0


if __name__ == "__main__":
    sys.exit(main())
