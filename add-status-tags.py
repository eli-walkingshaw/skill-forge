#!/usr/bin/env python3
"""Add Obsidian #tags to rejected/ and archive/ files.

Going forward, the watcher will inject tags into the Obsidian callout block
when moving files:
  - rejected by gates → #rejected #gate-failed
  - successfully installed → #archived #installed

Also backfills existing files in both folders so the tag pane shows them all.

Tags are added as an inline `> #rejected #gate-failed` line at the top of
the Obsidian `> [!info]` callout. Obsidian indexes inline `#tags` natively.

Run from inside ~/code/skill-forge:
    python3 add-status-tags.py
"""
import ast
import re
import sys
from pathlib import Path


# The new canonical process_approved_file. Same as the rewrite from before,
# but injects #archived #installed before the archive move, and #rejected
# #gate-failed before the rejected move.
CANONICAL_FN = '''def process_approved_file(config: Config, path: Path) -> None:
    """Process a single approved proposal file."""
    print(f"[watch] processing {path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[watch] read failed: {e}")
        return

    skill_md = extract_skill_md(text)
    if skill_md is None:
        print(f"[watch] couldn't find SKILL.md content in {path.name}")
        return

    ok, err, name = validate_skill(skill_md)
    if not ok:
        print(f"[watch] validation failed for {path.name}: {err}")
        return

    # ---- Gates: run quality/sensitivity/effectiveness checks before install ----
    import os as _os
    gates_enabled = _os.environ.get("GATES_ENABLED", "true").lower() == "true"
    if gates_enabled:
        from .gates import run_all_gates
        block_thin = _os.environ.get("GATES_BLOCK_THIN_DRAFTS", "true").lower() == "true"
        effect_enabled = _os.environ.get("GATES_EFFECTIVENESS_ENABLED", "true").lower() == "true"
        report = run_all_gates(
            skill_md,
            skill_name=name,
            block_thin_drafts=block_thin,
            effectiveness_enabled=effect_enabled,
            api_key=config.anthropic_api_key,
        )
        if not report.overall_passed:
            rejected_dir = config.vault_path / "rejected"
            rejected_dir.mkdir(parents=True, exist_ok=True)
            rejected_path = rejected_dir / path.name
            report_path = rejected_dir / (path.stem + ".gate-report.md")
            report_path.write_text(report.render(), encoding="utf-8")
            # Tag the proposal file with #rejected #gate-failed for Obsidian browsing.
            try:
                tagged = inject_tags_into_callout(
                    path.read_text(encoding="utf-8"),
                    ["#rejected", "#gate-failed"],
                )
                path.write_text(tagged, encoding="utf-8")
            except OSError:
                pass
            shutil.move(str(path), str(rejected_path))
            print(f"[watch] gates failed for {path.name} — moved to rejected/")
            print(f"[watch] see {report_path.name} for details")
            return
        print(f"[watch] gates passed ({len(report.results)} checks)")

    target = install_skill(config, name, skill_md)
    print(f"[watch] installed → {target}")

    try:
        git_commit_and_push(
            config,
            message=f"forge: add skill `{name}` (from {path.name})",
            paths=[target],
        )
        print(f"[watch] committed{' + pushed' if config.git_auto_push else ''}")
    except subprocess.CalledProcessError as e:
        print(f"[watch] git operation failed: {e}")
        return

    # Symlink the new skill into ~/.claude/skills/ so Claude Code picks it up.
    link_into_claude_skills(config, name)

    # Tag the approved proposal with #archived #installed before moving.
    try:
        tagged = inject_tags_into_callout(
            path.read_text(encoding="utf-8"),
            ["#archived", "#installed"],
        )
        path.write_text(tagged, encoding="utf-8")
    except OSError:
        pass

    archive_target = config.archive_dir / f"installed__{path.name}"
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), str(archive_target))
        print(f"[watch] archived → {archive_target.name}")
    except FileNotFoundError:
        print(f"[watch] (proposal already gone from approved/, skipping archive)")
'''


# The helper that injects tags into the callout block.
INJECT_FN = '''
def inject_tags_into_callout(text: str, tags: list[str]) -> str:
    """Insert an Obsidian-style tag line into the `> [!info]` callout block.

    The first line of a proposal looks like `> [!info] skill-forge proposal`.
    We insert a tag line immediately after it: `> #tag1 #tag2`. If the tags
    are already present, this is a no-op (idempotent).
    """
    if not tags:
        return text

    lines = text.splitlines()
    if not lines or not lines[0].lstrip().startswith("> [!info]"):
        # No callout to inject into — return unchanged.
        return text

    # Check if any of the tags are already present anywhere in the callout.
    # Find the end of the callout (first non-`>`-prefixed line).
    callout_end = 1
    while callout_end < len(lines) and (
        lines[callout_end].startswith(">") or lines[callout_end].strip() == ""
    ):
        if not lines[callout_end].startswith(">") and lines[callout_end].strip() == "":
            break
        callout_end += 1
    callout_text = "\\n".join(lines[:callout_end])

    # Idempotent: if the exact tag line is already there, skip.
    tag_line = "> " + " ".join(tags)
    for line in lines[:callout_end]:
        if line.strip() == tag_line.strip():
            return text  # already present

    # Insert the tag line right after the `> [!info]` line.
    new_lines = [lines[0], tag_line] + lines[1:]
    result = "\\n".join(new_lines)
    if text.endswith("\\n"):
        result += "\\n"
    return result
'''


def main() -> int:
    watch_path = Path("forge/watch.py")
    if not watch_path.exists():
        print("X forge/watch.py not found — run from ~/code/skill-forge")
        return 1

    s = watch_path.read_text()

    # ---- Step 1: replace process_approved_file with the tag-injecting version ----
    start_re = re.compile(r"^def process_approved_file\b", re.MULTILINE)
    start_match = start_re.search(s)
    if not start_match:
        print("X couldn't find `def process_approved_file`")
        return 1
    start = start_match.start()
    next_def_re = re.compile(r"^(def |class )", re.MULTILINE)
    next_match = next_def_re.search(s, start + 1)
    end = next_match.start() if next_match else len(s)

    s = s[:start] + CANONICAL_FN + "\n\n" + s[end:]
    print("  + replaced process_approved_file with tag-injecting version")

    # ---- Step 2: add inject_tags_into_callout helper if missing ----
    if "def inject_tags_into_callout" not in s:
        # Insert just before process_approved_file
        idx = s.find("def process_approved_file")
        s = s[:idx] + INJECT_FN.lstrip() + "\n\n" + s[idx:]
        print("  + added inject_tags_into_callout() helper")
    else:
        print("  + inject_tags_into_callout() already present")

    # Parse-check before writing
    try:
        ast.parse(s)
    except SyntaxError as e:
        print(f"X resulting watch.py has syntax error: {e}")
        return 1

    watch_path.write_text(s)
    print("  + watch.py written")

    # Clear .pyc cache to guarantee fresh load
    pycache = Path("forge/__pycache__")
    if pycache.exists():
        cleared = 0
        for pyc in pycache.glob("watch.*.pyc"):
            pyc.unlink()
            cleared += 1
        if cleared:
            print(f"  + cleared {cleared} cached .pyc file(s)")

    # ---- Step 3: backfill existing files in rejected/ and archive/ ----
    print()
    print("Backfilling existing files...")

    # Need config to find vault path. Don't import (avoids the heavy chain) —
    # just parse .env directly.
    env_vars = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()

    vault_path = env_vars.get("VAULT_PATH")
    if not vault_path:
        print("  X VAULT_PATH not in .env — can't backfill")
        return 1

    vault = Path(vault_path).expanduser()

    # Import the new helper from the just-patched watch.py
    sys.path.insert(0, ".")
    # Force reimport in case it was already loaded
    if "forge.watch" in sys.modules:
        del sys.modules["forge.watch"]
    # Stub out the heavy imports first so we can import watch.py standalone
    # Actually — just inline the helper here, simpler.

    def inject_tags(text: str, tags: list[str]) -> str:
        if not tags:
            return text
        lines = text.splitlines()
        if not lines or not lines[0].lstrip().startswith("> [!info]"):
            return text
        callout_end = 1
        while callout_end < len(lines):
            if not lines[callout_end].startswith(">"):
                break
            callout_end += 1
        tag_line = "> " + " ".join(tags)
        for line in lines[:callout_end]:
            if line.strip() == tag_line.strip():
                return text
        new_lines = [lines[0], tag_line] + lines[1:]
        result = "\n".join(new_lines)
        if text.endswith("\n"):
            result += "\n"
        return result

    backfilled = 0
    skipped = 0

    for folder_name, tags in [
        ("rejected", ["#rejected", "#gate-failed"]),
        ("archive", ["#archived", "#installed"]),
    ]:
        folder = vault / folder_name
        if not folder.exists():
            print(f"  ~ {folder_name}/ doesn't exist yet — skipping")
            continue

        for md in folder.glob("*.md"):
            # Skip .gate-report.md files — they aren't proposals
            if ".gate-report.md" in md.name:
                continue
            try:
                original = md.read_text(encoding="utf-8")
                tagged = inject_tags(original, tags)
                if tagged != original:
                    md.write_text(tagged, encoding="utf-8")
                    backfilled += 1
                    print(f"  + {folder_name}/{md.name}")
                else:
                    skipped += 1
            except OSError as e:
                print(f"  X {folder_name}/{md.name}: {e}")

    print(f"\n  backfilled {backfilled} file(s), skipped {skipped} (already tagged or no callout)")

    # ---- Verify ----
    print()
    print("Verification:")
    final = watch_path.read_text()
    helper_count = final.count("def inject_tags_into_callout")
    fn_count = final.count("def process_approved_file")
    rejected_tag_count = final.count('"#rejected"')
    archived_tag_count = final.count('"#archived"')
    print(f"  inject_tags_into_callout defs: {helper_count} (want 1)")
    print(f"  process_approved_file defs:    {fn_count} (want 1)")
    print(f"  '#rejected' tag in source:     {rejected_tag_count} (want >= 1)")
    print(f"  '#archived' tag in source:     {archived_tag_count} (want >= 1)")

    if not (helper_count == 1 and fn_count == 1 and rejected_tag_count >= 1 and archived_tag_count >= 1):
        print("X unexpected count — check forge/watch.py manually")
        return 1

    print()
    print("+ done")
    print()
    print("Restart the watcher so the new code is loaded:")
    print("  pkill -9 -f 'forge watch'")
    print("  sleep 1")
    print("  cd ~/code/skill-forge")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    print()
    print("In Obsidian, hit Cmd+R. The tag pane should now show #rejected, #gate-failed,")
    print("#archived, and #installed with file counts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
