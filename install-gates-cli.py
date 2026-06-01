#!/usr/bin/env python3
"""Add `forge gates <path>` subcommand — run gates on any SKILL.md file.

Currently gates only fire from inside the watcher's process_approved_file flow.
This patch exposes them as a standalone CLI subcommand so they can be:
  - Run manually before promoting a pending skill
  - Run from GitHub Actions CI on pull requests
  - Run by a teammate inspecting their own SKILL.md before pushing

Usage:
    python3 -m forge gates path/to/SKILL.md
    python3 -m forge gates path/to/SKILL.md --no-effectiveness  # skip the slow/expensive Claude call
    python3 -m forge gates path/to/SKILL.md --json              # machine-readable output

Exit codes:
    0 — all gates passed
    1 — one or more gates failed
    2 — argument or file error (not a gate failure)

Handles both shapes:
    1. Bare SKILL.md (just frontmatter + body) — used in the live repo
    2. Wrapper-frontmatter proposal — used in approved/, pending/

For shape #2, the SKILL.md content is extracted from after the divider.

Run from inside ~/code/skill-forge to install:
    python3 install-gates-cli.py
"""
import ast
import re
import sys
from pathlib import Path


COMMANDS_PY_ADDITIONS = '''

# ---------- forge gates ----------------------------------------------------


def cmd_gates(args, config: Config) -> int:
    """Run gates on a SKILL.md file. Returns 0 if all pass, 1 if any fail."""
    import json as _json
    import re as _re
    from .gates import run_all_gates

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"X file not found: {path}", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"X not a file: {path}", file=sys.stderr)
        return 2

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"X couldn't read {path}: {e}", file=sys.stderr)
        return 2

    # Detect shape: bare SKILL.md vs wrapper-frontmatter proposal.
    head_lines = text.splitlines()[:50]
    sep_count = sum(1 for line in head_lines if line.rstrip() == "---")
    skill_md = text
    if sep_count >= 4:
        # Wrapper proposal: extract everything after the divider
        divider_match = _re.search(r"\\n---\\s*\\n---\\s*\\n", text)
        if divider_match:
            skill_md = "---\\n" + text[divider_match.end():]

    # Derive skill name from frontmatter (or filename fallback)
    name_match = _re.search(r"^name:\\s*(\\S+)", skill_md, _re.MULTILINE)
    skill_name = name_match.group(1).strip() if name_match else path.stem

    # Run the gates
    effect_enabled = not args.no_effectiveness
    if effect_enabled and not config.anthropic_api_key:
        print("! ANTHROPIC_API_KEY not set — effectiveness gate will fail", file=sys.stderr)

    report = run_all_gates(
        skill_md,
        skill_name=skill_name,
        block_thin_drafts=not args.allow_thin_drafts,
        effectiveness_enabled=effect_enabled,
        api_key=config.anthropic_api_key,
    )

    if args.json:
        out = {
            "skill_name": report.skill_name,
            "overall_passed": report.overall_passed,
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "findings": r.findings,
                    "suggestions": r.suggestions,
                }
                for r in report.results
            ],
        }
        print(_json.dumps(out, indent=2))
    else:
        print(report.render())

    return 0 if report.overall_passed else 1
'''


MAIN_PY_ADDITIONS = """
    gates_p = sub.add_parser("gates", help="Run gates on a SKILL.md file (returns 0=pass, 1=fail)")
    gates_p.add_argument("path", help="Path to SKILL.md (bare or wrapped proposal)")
    gates_p.add_argument("--no-effectiveness", action="store_true",
                         help="Skip the effectiveness gate (no Claude API call)")
    gates_p.add_argument("--allow-thin-drafts", action="store_true",
                         help="Don't block thin drafts in the quality gate")
    gates_p.add_argument("--json", action="store_true",
                         help="Output machine-readable JSON instead of human report")
    gates_p.set_defaults(fn=cmd_gates)
"""


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge", file=sys.stderr)
        return 1

    commands_path = forge_dir / "commands.py"
    main_path = forge_dir / "__main__.py"

    # 1. Add cmd_gates to commands.py
    cmds = commands_path.read_text()
    if "def cmd_gates(" in cmds:
        print("  + cmd_gates already in commands.py")
    else:
        cmds = cmds.rstrip() + "\n" + COMMANDS_PY_ADDITIONS
        commands_path.write_text(cmds)
        print("  + added cmd_gates to commands.py")

    # 2. Wire into __main__.py
    main_src = main_path.read_text()

    # Add cmd_gates to imports
    if "cmd_gates" not in main_src:
        import_re = re.compile(r"(from \.commands import \(\n)(.*?)(\n\))", re.DOTALL)
        m = import_re.search(main_src)
        if m:
            body = m.group(2)
            if not body.rstrip().endswith(","):
                body = body.rstrip() + ","
            new_body = body + "\n    cmd_gates,"
            new_import = m.group(1) + new_body + m.group(3)
            main_src = main_src[:m.start()] + new_import + main_src[m.end():]
            print("  + added cmd_gates import to __main__.py")
        else:
            print("  ! couldn't find import block in __main__.py — manual edit needed")
            return 1

    # Add subparser
    if 'gates_p = sub.add_parser("gates"' not in main_src:
        marker = "    return p"
        if marker in main_src:
            main_src = main_src.replace(marker, MAIN_PY_ADDITIONS + "\n" + marker, 1)
            main_path.write_text(main_src)
            print("  + added gates subparser to __main__.py")
        else:
            print("  ! couldn't find `return p` marker in __main__.py")
            return 1
    else:
        print("  + gates subparser already in __main__.py")
        main_path.write_text(main_src)

    # Parse-check
    for p in [commands_path, main_path]:
        try:
            ast.parse(p.read_text())
        except SyntaxError as e:
            print(f"X syntax error in {p}: {e}", file=sys.stderr)
            return 1
    print("  + all files parse cleanly")

    # Clear pyc cache
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()

    print()
    print("+ done")
    print()
    print("Try:")
    print("  python3 -m forge gates ~/code/torus-skills/data/dbt-test-models/SKILL.md")
    print("  python3 -m forge gates <path> --no-effectiveness   # skip Claude call")
    print("  python3 -m forge gates <path> --json               # machine-readable")
    print()
    print("Exit code 0 = all gates passed, 1 = something failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
