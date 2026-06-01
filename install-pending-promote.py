#!/usr/bin/env python3
"""Add `forge pending promote` subcommand — graduate a pending skill to a stack.

Two modes:
  forge pending promote <skill>              # opens a PR via `gh` CLI
  forge pending promote <skill> --direct     # commits + pushes to main directly

Flags:
  --stack <name>       Override stack from frontmatter (e.g. promote to engineering)
  --skip-gates         Don't run gates before promoting (dangerous; use sparingly)
  --no-effectiveness   Skip effectiveness gate (no Claude API call)

The promote flow:
  1. Validate pending/<skill>/SKILL.md exists
  2. Run gates (unless --skip-gates)
  3. Determine target stack from frontmatter or --stack
  4. Default mode: create branch `promote/<skill>`, git mv, commit, push, gh pr create
  5. --direct mode: git mv on main, commit, push to main

Requires `gh` CLI authenticated (only for default mode; --direct doesn't need it).

Run from inside ~/code/skill-forge:
    python3 install-pending-promote.py
"""
import ast
import re
import sys
from pathlib import Path


# This gets appended to commands.py — added as a function that cmd_pending can dispatch to.
PROMOTE_FN_CODE = '''


def _pending_promote(config: Config, pending_dir: Path, name: str,
                     stack_override: str = "", skip_gates: bool = False,
                     effectiveness: bool = True, direct: bool = False) -> int:
    """Promote a pending skill to a stack via PR (default) or direct push (--direct)."""
    import subprocess as _sp
    import shutil as _shutil

    skill_dir = pending_dir / name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(f"X not found: {skill_md}", file=sys.stderr)
        print(f"  (try `forge pending list` to see what's available)", file=sys.stderr)
        return 1

    # Step 1: Gates
    if not skip_gates:
        try:
            from .gates import run_all_gates
            text = skill_md.read_text(encoding="utf-8")
            report = run_all_gates(
                text,
                skill_name=name,
                block_thin_drafts=True,
                effectiveness_enabled=effectiveness,
                api_key=config.anthropic_api_key,
            )
            if not report.overall_passed:
                print(report.render())
                print()
                print(f"X gates failed for {name}. Fix the SKILL.md or run with --skip-gates.",
                      file=sys.stderr)
                return 1
            print(f"  + gates passed for {name}")
        except Exception as e:
            print(f"  ! gate execution error: {e}", file=sys.stderr)
            print(f"  (use --skip-gates to bypass if you've already validated)", file=sys.stderr)
            return 1
    else:
        print(f"  ! skipping gates per --skip-gates")

    # Step 2: Determine target stack
    fm = read_skill_frontmatter(skill_md)
    if stack_override:
        target_stack = stack_override
        print(f"  + target stack: {target_stack} (from --stack override)")
    else:
        stacks_raw = fm.get("stacks", "")
        # Parse `[data, engineering]` style or just `data`
        stacks_list = []
        m = re.search(r"\\[(.*?)\\]", str(stacks_raw))
        if m:
            stacks_list = [s.strip() for s in m.group(1).split(",") if s.strip()]
        elif stacks_raw:
            stacks_list = [str(stacks_raw).strip()]
        if not stacks_list:
            print(f"X no `stacks:` in frontmatter — use --stack to specify", file=sys.stderr)
            return 1
        # Canonical = first alphabetically (existing convention)
        target_stack = sorted(stacks_list)[0]
        print(f"  + target stack: {target_stack} (canonical from {stacks_list})")

    # Step 3: Verify target stack folder exists in repo
    repo = config.skills_repo_path
    target_stack_dir = repo / target_stack
    if not target_stack_dir.exists():
        print(f"X stack folder doesn't exist: {target_stack_dir}", file=sys.stderr)
        print(f"  (create it first: mkdir -p {target_stack_dir})", file=sys.stderr)
        return 1

    target_dir = target_stack_dir / name
    if target_dir.exists():
        print(f"X target already exists: {target_dir}", file=sys.stderr)
        print(f"  (existing skill with the same name — manual merge needed)", file=sys.stderr)
        return 1

    # Step 4: Branch creation (or main for --direct)
    if not (repo / ".git").exists():
        print(f"X {repo} is not a git repo", file=sys.stderr)
        return 1

    if direct:
        branch = "main"
        print(f"  + using main branch (--direct mode)")
    else:
        branch = f"promote/{name}"
        # Create the branch
        try:
            _sp.run(["git", "-C", str(repo), "checkout", "-b", branch], check=True,
                    capture_output=True, text=True)
            print(f"  + created branch: {branch}")
        except _sp.CalledProcessError as e:
            # Branch may already exist; try checkout
            try:
                _sp.run(["git", "-C", str(repo), "checkout", branch], check=True,
                        capture_output=True, text=True)
                print(f"  + switched to existing branch: {branch}")
            except _sp.CalledProcessError as e2:
                print(f"X couldn't create or switch to branch {branch}: {e2.stderr}",
                      file=sys.stderr)
                return 1

    # Step 5: git mv pending/<skill> → <stack>/<skill>
    try:
        rel_src = f"pending/{name}"
        rel_dst = f"{target_stack}/{name}"
        _sp.run(["git", "-C", str(repo), "mv", rel_src, rel_dst], check=True,
                capture_output=True, text=True)
        print(f"  + git mv {rel_src} → {rel_dst}")
    except _sp.CalledProcessError as e:
        print(f"X git mv failed: {e.stderr}", file=sys.stderr)
        # Cleanup: switch back to main if we created a branch
        if not direct:
            _sp.run(["git", "-C", str(repo), "checkout", "main"], capture_output=True)
        return 1

    # Step 6: commit
    try:
        msg = f"forge: promote {name} from pending/ to {target_stack}/"
        _sp.run(["git", "-C", str(repo), "commit", "-m", msg], check=True,
                capture_output=True, text=True)
        print(f"  + committed")
    except _sp.CalledProcessError as e:
        print(f"X commit failed: {e.stderr}", file=sys.stderr)
        return 1

    # Step 7: push
    push_args = ["git", "-C", str(repo), "push"]
    if not direct:
        push_args.extend(["-u", "origin", branch])
    try:
        result = _sp.run(push_args, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ! push failed: {result.stderr.strip()}", file=sys.stderr)
            print(f"    (you can push manually: cd {repo} && git push)", file=sys.stderr)
            return 1
        print(f"  + pushed")
    except Exception as e:
        print(f"  ! push error: {e}", file=sys.stderr)
        return 1

    # Step 8: For PR mode, create the PR via gh
    if not direct:
        # Build PR body from skill description
        description = fm.get("description", "")
        pr_body = f"Promoting `{name}` from `pending/` to `{target_stack}/`.\\n\\n"
        if description:
            pr_body += f"**Description:** {description}\\n\\n"
        pr_body += f"Gates: {'passed' if not skip_gates else 'SKIPPED via --skip-gates'}\\n"
        pr_body += f"\\n_Auto-generated by `forge pending promote`._"

        try:
            result = _sp.run(
                ["gh", "pr", "create",
                 "--title", f"forge: promote {name} to {target_stack}",
                 "--body", pr_body,
                 "--head", branch],
                cwd=str(repo),
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                pr_url = result.stdout.strip()
                print(f"  + opened PR: {pr_url}")
                # Switch back to main so we're not stuck on the branch
                _sp.run(["git", "-C", str(repo), "checkout", "main"], capture_output=True)
                print(f"  + switched back to main")
            else:
                print(f"  ! gh pr create failed: {result.stderr.strip()}", file=sys.stderr)
                print(f"    (the branch was pushed; open the PR manually on GitHub)",
                      file=sys.stderr)
                return 1
        except FileNotFoundError:
            print(f"  ! gh CLI not found — install with `brew install gh`", file=sys.stderr)
            print(f"    (the branch {branch} was pushed; open the PR manually)",
                  file=sys.stderr)
            return 1
    else:
        # --direct mode: we're done, no PR needed
        pass

    print()
    print(f"+ done")
    if direct:
        print(f"  {name} is now live in {target_stack}/")
        print(f"  Next: watcher will symlink it into ~/.claude/skills/ automatically")
    else:
        print(f"  PR opened — review and merge to make {name} live in {target_stack}/")
    return 0
'''


# This replaces the existing cmd_pending dispatcher to add the promote case.
# We splice promote handling into the existing if-elif chain.
DISPATCH_REPLACEMENT_OLD = '''    elif sub_cmd == "reject":
        return _pending_reject(config, pending_dir, args.name)
    else:'''

DISPATCH_REPLACEMENT_NEW = '''    elif sub_cmd == "reject":
        return _pending_reject(config, pending_dir, args.name)
    elif sub_cmd == "promote":
        return _pending_promote(
            config, pending_dir, args.name,
            stack_override=getattr(args, "stack", "") or "",
            skip_gates=getattr(args, "skip_gates", False),
            effectiveness=not getattr(args, "no_effectiveness", False),
            direct=getattr(args, "direct", False),
        )
    else:'''


# Add a `promote` subparser inside the existing pending_sub group.
# We insert this right after the `reject_p` lines in __main__.py.
MAIN_PROMOTE_LINES = '''    promote_p = pending_sub.add_parser("promote", help="Promote a pending skill to a stack (opens PR by default)")
    promote_p.add_argument("name", help="Skill folder name in pending/")
    promote_p.add_argument("--stack", help="Override target stack (default: from frontmatter)")
    promote_p.add_argument("--direct", action="store_true",
                           help="Skip PR, commit + push directly to main")
    promote_p.add_argument("--skip-gates", action="store_true",
                           help="Don't run gates before promoting (dangerous)")
    promote_p.add_argument("--no-effectiveness", action="store_true",
                           help="Skip effectiveness gate (no Claude call)")
'''


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge", file=sys.stderr)
        return 1

    commands_path = forge_dir / "commands.py"
    main_path = forge_dir / "__main__.py"

    # 1. Add _pending_promote function to commands.py
    cmds = commands_path.read_text()
    if "def _pending_promote(" in cmds:
        print("  + _pending_promote already in commands.py")
    else:
        cmds = cmds.rstrip() + "\n" + PROMOTE_FN_CODE
        commands_path.write_text(cmds)
        print("  + added _pending_promote to commands.py")

    # 2. Update cmd_pending dispatcher to include promote case
    cmds = commands_path.read_text()
    if 'sub_cmd == "promote"' in cmds:
        print("  + cmd_pending dispatcher already has promote case")
    elif DISPATCH_REPLACEMENT_OLD in cmds:
        cmds = cmds.replace(DISPATCH_REPLACEMENT_OLD, DISPATCH_REPLACEMENT_NEW, 1)
        commands_path.write_text(cmds)
        print("  + wired promote case into cmd_pending dispatcher")
    else:
        print("X couldn't find dispatcher to update", file=sys.stderr)
        return 1

    # 3. Add promote subparser to __main__.py
    main_src = main_path.read_text()
    if 'promote_p = pending_sub.add_parser("promote"' in main_src:
        print("  + promote subparser already in __main__.py")
    else:
        # Insert right after the reject_p add_argument lines, before pending_p.set_defaults
        marker = '    pending_p.set_defaults(fn=cmd_pending)'
        if marker not in main_src:
            print(f"X couldn't find marker `{marker}` in __main__.py", file=sys.stderr)
            return 1
        main_src = main_src.replace(marker, MAIN_PROMOTE_LINES + marker, 1)
        main_path.write_text(main_src)
        print("  + added promote subparser to __main__.py")

    # Parse check
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
    print("  python3 -m forge pending promote <skill>                       # PR mode (default)")
    print("  python3 -m forge pending promote <skill> --direct              # commit to main")
    print("  python3 -m forge pending promote <skill> --stack engineering   # override stack")
    print("  python3 -m forge pending promote <skill> --skip-gates          # bypass gates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
