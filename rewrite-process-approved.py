#!/usr/bin/env python3
"""Replace process_approved_file in forge/watch.py wholesale.

The string-patching approach has failed multiple times because of subtle
indentation drift. This script identifies the existing function by its
signature line and replaces the entire function body with a known-good
version that has gates wired in correctly.

Run from inside ~/code/skill-forge:
    python3 rewrite-process-approved.py
"""
import ast
import re
import sys
from pathlib import Path


# The canonical, correct function. Indentation is exactly 4 spaces for the body.
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

    # Move the approved proposal into archive so it doesn't re-process.
    archive_target = config.archive_dir / f"installed__{path.name}"
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), str(archive_target))
        print(f"[watch] archived → {archive_target.name}")
    except FileNotFoundError:
        # File was already moved/deleted (e.g. by Obsidian's drag handler).
        # Not an error — the skill was already installed and committed.
        print(f"[watch] (proposal already gone from approved/, skipping archive)")
'''


def main() -> int:
    watch_path = Path("forge/watch.py")
    if not watch_path.exists():
        print("X forge/watch.py not found — run from ~/code/skill-forge")
        return 1

    s = watch_path.read_text()

    # Find the start of `def process_approved_file`
    start_re = re.compile(r"^def process_approved_file\b", re.MULTILINE)
    start_match = start_re.search(s)
    if not start_match:
        print("X couldn't find `def process_approved_file` in watch.py")
        return 1
    start = start_match.start()

    # Find the end of the function: the next top-level `def ` or end of file.
    # Top-level means at column 0.
    next_def_re = re.compile(r"^(def |class )", re.MULTILINE)
    next_match = next_def_re.search(s, start + 1)
    end = next_match.start() if next_match else len(s)

    # Show what we're about to remove (for the user's peace of mind)
    removed_lines = s[start:end].count("\n")
    print(f"  found process_approved_file at offset {start}–{end} ({removed_lines} lines)")

    # Replace the whole function block
    new_s = s[:start] + CANONICAL_FN + "\n\n" + s[end:]

    # Clean up potential extra blank lines from the join
    new_s = re.sub(r"\n{3,}", "\n\n\n", new_s)

    # Parse-check before writing
    try:
        ast.parse(new_s)
    except SyntaxError as e:
        print(f"X canonical function caused syntax error: {e}")
        return 1

    watch_path.write_text(new_s)
    print(f"  + replaced process_approved_file with canonical version")

    # Also clear .pyc cache to guarantee Python reloads fresh
    pycache = Path("forge/__pycache__")
    if pycache.exists():
        cleared = 0
        for pyc in pycache.glob("watch.*.pyc"):
            pyc.unlink()
            cleared += 1
        if cleared:
            print(f"  + cleared {cleared} cached .pyc file(s)")

    # Verify by re-reading and parsing the function
    print()
    print("Verification:")
    final = watch_path.read_text()

    # Count gate blocks (must be exactly 1)
    gate_block_count = final.count("Gates: run quality")
    print(f"  gate block count: {gate_block_count} (want 1)")

    # Find the comment and check its column
    for i, line in enumerate(final.splitlines(), start=1):
        if "Gates: run quality" in line:
            indent = len(line) - len(line.lstrip())
            print(f"  gate block at column {indent} (line {i}) {'+' if indent == 4 else '- WRONG'}")
            break

    # Confirm parse
    try:
        tree = ast.parse(final)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "process_approved_file":
                # Look for the gates call as a function call inside this function
                found_gates = any(
                    isinstance(n, ast.Call) and (
                        (isinstance(n.func, ast.Name) and n.func.id == "run_all_gates")
                        or (isinstance(n.func, ast.Attribute) and n.func.attr == "run_all_gates")
                    )
                    for n in ast.walk(node)
                )
                print(f"  AST: run_all_gates called inside process_approved_file: {'+' if found_gates else '- NO'}")
                break
    except SyntaxError as e:
        print(f"  X parse failed: {e}")
        return 1

    if gate_block_count != 1:
        print()
        print("X gate block count is wrong")
        return 1

    print()
    print("+ done")
    print()
    print("NOW restart the watcher cleanly:")
    print("  pkill -9 -f 'forge watch'")
    print("  sleep 1")
    print("  ps aux | grep -v grep | grep 'forge watch'")
    print("  # should be empty before continuing")
    print("  cd ~/code/skill-forge")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    print("  sleep 2")
    print("  tail ~/.skill-forge/watch.log")
    print("  # should show: [watch] watching .../approved (poll every 2.0s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
