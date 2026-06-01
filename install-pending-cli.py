#!/usr/bin/env python3
"""Add `forge pending` subcommand with three actions: list, view, reject.

This is the read/triage half of the pending/ workflow. The promote half
(opening PRs, running gates, doing the git mv) is patch #4.

Mirrors the existing `forge stack` nested-subcommand pattern:
  - One `cmd_pending(args, config)` dispatch function in commands.py
  - Internal branching on args.pending_cmd
  - Parser uses add_subparsers(dest="pending_cmd")

Subcommands:
  forge pending list                Show what's in pending/
  forge pending view <skill>        Print SKILL.md to stdout
  forge pending reject <skill>      rm -rf + commit + push (curator only)

Run from inside ~/code/skill-forge:
    python3 install-pending-cli.py
"""
import ast
import re
import sys
from pathlib import Path


COMMANDS_PY_ADDITIONS = '''

# ---------- forge pending --------------------------------------------------


def cmd_pending(args, config: Config) -> int:
    """Dispatch for `forge pending <subcommand>`."""
    import shutil as _shutil
    import subprocess as _sp

    pending_dir = config.skills_repo_path / "pending"

    sub_cmd = getattr(args, "pending_cmd", None)

    if sub_cmd == "list" or sub_cmd is None:
        return _pending_list(pending_dir)
    elif sub_cmd == "view":
        return _pending_view(pending_dir, args.name)
    elif sub_cmd == "reject":
        return _pending_reject(config, pending_dir, args.name)
    else:
        print(f"X unknown pending subcommand: {sub_cmd}", file=sys.stderr)
        return 1


def _pending_list(pending_dir: Path) -> int:
    """List skills currently in pending/."""
    if not pending_dir.exists():
        print("(no pending/ folder — nothing pending)")
        return 0
    skills = []
    for entry in sorted(pending_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        # Pull a description for the listing
        desc = ""
        try:
            text = skill_md.read_text(encoding="utf-8")
            m = __import__("re").search(r"^description:\\s*(.+)$", text, __import__("re").MULTILINE)
            if m:
                desc = m.group(1).strip()
                if len(desc) > 100:
                    desc = desc[:97] + "..."
        except OSError:
            pass
        skills.append((entry.name, desc))

    if not skills:
        print("(pending/ is empty — no skills awaiting curation)")
        return 0

    print(f"{len(skills)} skill(s) in pending/:")
    print()
    for name, desc in skills:
        print(f"  {name}")
        if desc:
            print(f"      {desc}")
    print()
    print("Next steps:")
    print("  forge pending view <name>      # inspect SKILL.md")
    print("  forge pending reject <name>    # remove from pending/")
    print("  forge pending promote <name>   # move to a stack (patch #4)")
    return 0


def _pending_view(pending_dir: Path, name: str) -> int:
    """Print the SKILL.md of a pending skill to stdout."""
    skill_md = pending_dir / name / "SKILL.md"
    if not skill_md.exists():
        print(f"X not found: {skill_md}", file=sys.stderr)
        print(f"  (try `forge pending list` to see available)", file=sys.stderr)
        return 1
    try:
        print(skill_md.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"X couldn't read {skill_md}: {e}", file=sys.stderr)
        return 1
    return 0


def _pending_reject(config: Config, pending_dir: Path, name: str) -> int:
    """Delete a pending skill folder and commit the removal."""
    import shutil as _shutil
    import subprocess as _sp

    target = pending_dir / name
    if not target.exists():
        print(f"X not found: {target}", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"X not a directory: {target}", file=sys.stderr)
        return 1

    # Confirm with the user — destructive
    answer = input(f"Delete pending/{name}/? (yes/no): ").strip().lower()
    if answer not in ("yes", "y"):
        print("Cancelled.")
        return 0

    _shutil.rmtree(target)
    print(f"  + deleted pending/{name}/")

    # Commit + push if it's a git repo
    repo = config.skills_repo_path
    if (repo / ".git").exists():
        try:
            _sp.run(["git", "-C", str(repo), "add", "-A"], check=True)
            result = _sp.run(
                ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            )
            if result.returncode != 0:
                _sp.run(
                    ["git", "-C", str(repo), "commit", "-m", f"forge: reject pending/{name}"],
                    check=True,
                )
                print(f"  + committed")
                if getattr(config, "git_auto_push", True):
                    push_result = _sp.run(
                        ["git", "-C", str(repo), "push"],
                        check=False,
                    )
                    if push_result.returncode == 0:
                        print(f"  + pushed")
                    else:
                        print(f"  ! push failed (manual `git push` needed)", file=sys.stderr)
        except _sp.CalledProcessError as e:
            print(f"  ! git operation failed: {e}", file=sys.stderr)
            print(f"  (the folder was deleted; commit it manually with:", file=sys.stderr)
            print(f"     git -C {repo} add -A && git -C {repo} commit -m 'reject {name}')",
                  file=sys.stderr)
    return 0
'''


MAIN_PY_ADDITIONS = """
    pending_p = sub.add_parser("pending", help="Manage skills awaiting curation in pending/")
    pending_sub = pending_p.add_subparsers(dest="pending_cmd")
    pending_sub.add_parser("list", help="List skills in pending/")
    view_p = pending_sub.add_parser("view", help="Print a pending skill's SKILL.md")
    view_p.add_argument("name", help="Skill folder name in pending/")
    reject_p = pending_sub.add_parser("reject", help="Delete a pending skill (irreversible)")
    reject_p.add_argument("name", help="Skill folder name to reject")
    pending_p.set_defaults(fn=cmd_pending)
"""


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge", file=sys.stderr)
        return 1

    commands_path = forge_dir / "commands.py"
    main_path = forge_dir / "__main__.py"

    cmds = commands_path.read_text()
    if "def cmd_pending(" in cmds:
        print("  + cmd_pending already in commands.py")
    else:
        cmds = cmds.rstrip() + "\n" + COMMANDS_PY_ADDITIONS
        commands_path.write_text(cmds)
        print("  + added cmd_pending to commands.py")

    main_src = main_path.read_text()
    if "cmd_pending" not in main_src:
        import_re = re.compile(r"(from \.commands import \(\n)(.*?)(\n\))", re.DOTALL)
        m = import_re.search(main_src)
        if m:
            body = m.group(2)
            if not body.rstrip().endswith(","):
                body = body.rstrip() + ","
            new_body = body + "\n    cmd_pending,"
            new_import = m.group(1) + new_body + m.group(3)
            main_src = main_src[:m.start()] + new_import + main_src[m.end():]
            print("  + added cmd_pending import to __main__.py")
        else:
            print("X couldn't find import block in __main__.py")
            return 1

    if 'pending_p = sub.add_parser("pending"' not in main_src:
        marker = "    return p"
        if marker in main_src:
            main_src = main_src.replace(marker, MAIN_PY_ADDITIONS + "\n" + marker, 1)
            main_path.write_text(main_src)
            print("  + added pending subparser to __main__.py")
        else:
            print("X couldn't find `return p` marker in __main__.py")
            return 1
    else:
        print("  + pending subparser already in __main__.py")
        main_path.write_text(main_src)

    for p in [commands_path, main_path]:
        try:
            ast.parse(p.read_text())
        except SyntaxError as e:
            print(f"X syntax error in {p}: {e}", file=sys.stderr)
            return 1
    print("  + all files parse cleanly")

    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()

    print()
    print("+ done")
    print()
    print("Try:")
    print("  python3 -m forge pending list")
    print("  python3 -m forge pending view <skill-name>")
    print("  python3 -m forge pending reject <skill-name>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
