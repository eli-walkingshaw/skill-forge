#!/usr/bin/env python3
"""Install the `forge stack` command for grouping skills into team-scoped repos.

Adds:
  - forge/stacks.py — the stack discovery/diff/publish logic
  - `forge stack` command in __main__.py
  - `stacks:` and `never_publish:` frontmatter fields are now recognized

Stacks are discovered from frontmatter — no separate config file. Each SKILL.md
can declare:

  ---
  name: suiteql-cookbook
  description: ...
  stacks: [data, engineering]
  ---

A skill with `never_publish: true` is excluded from every stack regardless.

Repo URLs go in .env:
  STACK_REPO_engineering=git@github.com:eli/torus-skills-engineering.git
  STACK_REPO_data=git@github.com:eli/torus-skills-data.git
  STACK_REPO_operations=git@github.com:eli/torus-skills-ops.git

If the env var for a stack is empty/missing, publish generates the stack repo
locally at ~/code/torus-skills-<name>/ without pushing.

Run from inside ~/code/skill-forge:
    python3 install-stacks.py
"""
import ast
import re
import sys
from pathlib import Path


STACKS_PY = '''"""Stack discovery, diff, and publish.

A stack is a curated subset of your skills, derived from `stacks:` frontmatter
entries on individual SKILL.md files. Each stack publishes to its own
read-only-by-others git repo so teammates can clone just their slice.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


_FRONTMATTER_RE = re.compile(r"^---\\s*\\n(.*?)\\n---\\s*\\n", re.DOTALL)
_LIST_RE = re.compile(r"\\[(.*?)\\]")


def _parse_frontmatter(skill_path: Path) -> dict:
    """Best-effort YAML parser. Handles flat key: value and simple `[a, b]` lists."""
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # List? e.g. tags: [a, b, c]
        list_m = _LIST_RE.match(value)
        if list_m:
            inner = list_m.group(1)
            items = [t.strip().strip(chr(34) + chr(39) + chr(96) + " ") for t in inner.split(",")]
            out[key] = [i for i in items if i]
        elif value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


@dataclass
class Stack:
    name: str
    skills: list[Path] = field(default_factory=list)
    repo_url: str = ""             # from STACK_REPO_<name> env var; "" = local only

    @property
    def local_repo_path(self) -> Path:
        return Path.home() / "code" / f"torus-skills-{self.name}"


def discover_stacks(config: Config) -> dict[str, Stack]:
    """Walk the canonical skills repo, group skills by their `stacks:` field.

    Skills with `never_publish: true` (or `never_publish: 1`) are excluded.
    """
    repo = config.skills_repo_path
    if not repo.exists():
        return {}

    stacks: dict[str, Stack] = {}
    for child in sorted(repo.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        fm = _parse_frontmatter(skill_md)
        if fm.get("never_publish"):
            continue
        skill_stacks = fm.get("stacks") or []
        if isinstance(skill_stacks, str):
            skill_stacks = [skill_stacks]
        for sname in skill_stacks:
            sname = sname.strip()
            if not sname:
                continue
            if sname not in stacks:
                stacks[sname] = Stack(name=sname)
            stacks[sname].skills.append(skill_md)

    # Pull repo URLs from environment.
    for name, stack in stacks.items():
        env_key = f"STACK_REPO_{name}"
        stack.repo_url = os.environ.get(env_key, "").strip()

    return stacks


def list_skill_assignments(config: Config) -> dict[Path, list[str]]:
    """Return {skill_path: [stack names it belongs to]} for every skill in the repo."""
    repo = config.skills_repo_path
    out: dict[Path, list[str]] = {}
    if not repo.exists():
        return out
    for child in sorted(repo.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        fm = _parse_frontmatter(skill_md)
        if fm.get("never_publish"):
            out[skill_md] = ["(never_publish)"]
            continue
        ss = fm.get("stacks") or []
        if isinstance(ss, str):
            ss = [ss]
        out[skill_md] = ss
    return out


def diff_stack(config: Config, stack: Stack) -> dict:
    """What would change if we published this stack now.

    Returns: {"added": [...], "updated": [...], "removed": [...]}
    where paths are relative to the stack repo root.
    """
    target_dir = stack.local_repo_path
    diff = {"added": [], "updated": [], "removed": []}

    # Build the set of skills that should be in the published stack
    desired = {s.parent.name: s for s in stack.skills}

    # What's currently in the target stack repo?
    current = set()
    if target_dir.exists():
        for child in target_dir.iterdir():
            if child.is_dir() and (child / "SKILL.md").exists():
                current.add(child.name)

    for name, source_skill in desired.items():
        if name not in current:
            diff["added"].append(name)
        else:
            existing = target_dir / name / "SKILL.md"
            try:
                if existing.read_text() != source_skill.read_text():
                    diff["updated"].append(name)
            except OSError:
                diff["updated"].append(name)

    for name in current:
        if name not in desired:
            diff["removed"].append(name)

    return diff


def publish_stack(config: Config, stack: Stack, *, dry_run: bool = False) -> dict:
    """Generate the stack repo and (optionally) push.

    Returns the diff that was applied (so the caller can decide to notify).
    """
    target_dir = stack.local_repo_path
    diff = diff_stack(config, stack)

    if dry_run:
        return diff

    # Initialize/clone the target repo if needed
    if not target_dir.exists():
        if stack.repo_url:
            print(f"  cloning {stack.repo_url}")
            try:
                subprocess.run(
                    ["git", "clone", stack.repo_url, str(target_dir)],
                    check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError:
                # Remote may not exist yet — init locally and add remote
                print(f"  remote not found, initializing locally")
                target_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "-C", str(target_dir), "init", "-q", "-b", "main"], check=True)
                subprocess.run(
                    ["git", "-C", str(target_dir), "remote", "add", "origin", stack.repo_url],
                    check=True,
                )
        else:
            print(f"  no STACK_REPO_{stack.name} set — generating locally only")
            target_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(target_dir), "init", "-q", "-b", "main"], check=True)

    # Apply diff
    for name in diff["added"] + diff["updated"]:
        src_skill_dir = config.skills_repo_path / name
        dst_skill_dir = target_dir / name
        dst_skill_dir.mkdir(parents=True, exist_ok=True)
        for src_file in src_skill_dir.rglob("*"):
            if src_file.is_file() and not src_file.name.startswith("."):
                rel = src_file.relative_to(src_skill_dir)
                dst = dst_skill_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)

    for name in diff["removed"]:
        rm_dir = target_dir / name
        if rm_dir.exists():
            shutil.rmtree(rm_dir)

    # Generate stack README
    readme = target_dir / "README.md"
    readme_lines = [
        f"# torus-skills-{stack.name}",
        "",
        f"Curated subset of Torus skills for the {stack.name} stack.",
        "",
        "Generated by skill-forge — do not hand-edit.",
        "",
        "## Skills in this stack",
        "",
    ]
    for skill_md in sorted(stack.skills, key=lambda p: p.parent.name):
        fm = _parse_frontmatter(skill_md)
        name = fm.get("name", skill_md.parent.name)
        desc = fm.get("description", "")
        readme_lines.append(f"- **{name}** — {desc[:120]}")
    readme.write_text("\\n".join(readme_lines) + "\\n", encoding="utf-8")

    # Configure git user if needed
    try:
        subprocess.run(
            ["git", "-C", str(target_dir), "config", "user.email"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(["git", "-C", str(target_dir), "config", "user.email", "skill-forge@local"], check=True)
        subprocess.run(["git", "-C", str(target_dir), "config", "user.name", "skill-forge"], check=True)

    # Commit
    subprocess.run(["git", "-C", str(target_dir), "add", "-A"], check=True)
    result = subprocess.run(
        ["git", "-C", str(target_dir), "diff", "--cached", "--quiet"],
    )
    if result.returncode != 0:
        msg = _commit_message(stack.name, diff)
        subprocess.run(["git", "-C", str(target_dir), "commit", "-q", "-m", msg], check=True)
        if stack.repo_url:
            try:
                subprocess.run(
                    ["git", "-C", str(target_dir), "push", "-u", "origin", "main"],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                print(f"  push failed: {e}")

    return diff


def _commit_message(stack_name: str, diff: dict) -> str:
    parts = []
    if diff["added"]:
        parts.append(f"add {len(diff['added'])}")
    if diff["updated"]:
        parts.append(f"update {len(diff['updated'])}")
    if diff["removed"]:
        parts.append(f"remove {len(diff['removed'])}")
    body = ", ".join(parts) or "no changes"
    return f"forge stack publish {stack_name}: {body}"


def set_skill_stacks(skill_md_path: Path, stacks: list[str]) -> bool:
    """Rewrite a SKILL.md's `stacks:` frontmatter field.

    Returns True if the file was changed, False if no change was needed.
    """
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return False

    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False

    fm_text = m.group(1)
    body_text = text[m.end():]

    new_line = f"stacks: [{', '.join(stacks)}]" if stacks else None

    fm_lines = fm_text.splitlines()
    out_lines = []
    seen_stacks_line = False
    inserted = False
    for line in fm_lines:
        if line.lstrip().startswith("stacks:"):
            seen_stacks_line = True
            if new_line:
                out_lines.append(new_line)
                inserted = True
            # otherwise skip the line — removing it
        else:
            out_lines.append(line)

    # If we have stacks and didn't find an existing line, insert before the closing of fm
    if new_line and not inserted:
        # Insert after `description:` if present, else at end
        desc_idx = next(
            (i for i, l in enumerate(out_lines) if l.lstrip().startswith("description:")),
            -1,
        )
        if desc_idx >= 0:
            out_lines.insert(desc_idx + 1, new_line)
        else:
            out_lines.append(new_line)

    new_text = "---\\n" + "\\n".join(out_lines) + "\\n---\\n" + body_text
    if new_text == text:
        return False
    skill_md_path.write_text(new_text, encoding="utf-8")
    return True
'''


# Command handlers added to commands.py
COMMANDS_PY_ADDITIONS = '''

# ---------- forge stack ----------------------------------------------------


def cmd_stack(args, config: Config) -> int:
    """Dispatch to the right sub-subcommand."""
    from .stacks import (
        discover_stacks,
        diff_stack,
        publish_stack,
        list_skill_assignments,
        set_skill_stacks,
    )

    sub = args.stack_cmd or "list"

    if sub == "list":
        stacks = discover_stacks(config)
        if not stacks:
            print("(no stacks defined — add `stacks: [name]` to SKILL.md frontmatter, or run `forge stack assign`)")
            return 0
        print(f"{len(stacks)} stack(s):\\n")
        for name, stack in sorted(stacks.items()):
            url = stack.repo_url or "(local only — no STACK_REPO_" + name + ")"
            print(f"  {name}  ({len(stack.skills)} skills)")
            print(f"    repo:  {url}")
            print(f"    local: {stack.local_repo_path}")
            for s in stack.skills[:5]:
                print(f"      - {s.parent.name}")
            if len(stack.skills) > 5:
                print(f"      ... and {len(stack.skills) - 5} more")
            print()
        return 0

    if sub == "diff":
        if not args.name:
            print("usage: forge stack diff <name>")
            return 1
        stacks = discover_stacks(config)
        stack = stacks.get(args.name)
        if not stack:
            print(f"no stack named '{args.name}' (known: {', '.join(stacks.keys()) or 'none'})")
            return 1
        diff = diff_stack(config, stack)
        print(f"stack: {args.name}")
        print(f"  added:   {len(diff['added'])} {diff['added']}")
        print(f"  updated: {len(diff['updated'])} {diff['updated']}")
        print(f"  removed: {len(diff['removed'])} {diff['removed']}")
        return 0

    if sub == "publish":
        stacks = discover_stacks(config)
        if args.all:
            targets = list(stacks.values())
        elif args.name:
            target = stacks.get(args.name)
            if not target:
                print(f"no stack named '{args.name}'")
                return 1
            targets = [target]
        else:
            print("usage: forge stack publish <name> | --all")
            return 1

        for stack in targets:
            print(f"\\n→ publishing {stack.name}...")
            diff = publish_stack(config, stack)
            print(f"  added: {len(diff['added'])}, updated: {len(diff['updated'])}, removed: {len(diff['removed'])}")
            if diff["added"]:
                # "Major change" — print the notification we'd send to Slack
                print(f"  NOTIFY: stack '{stack.name}' added {len(diff['added'])} new skill(s): {', '.join(diff['added'])}")
        return 0

    if sub == "assign":
        return _cmd_stack_assign_interactive(config)

    print(f"unknown stack subcommand: {sub}")
    return 1


def _cmd_stack_assign_interactive(config: Config) -> int:
    """Walk through every installed skill, ask which stacks it belongs to."""
    from .stacks import list_skill_assignments, set_skill_stacks
    assignments = list_skill_assignments(config)
    if not assignments:
        print("no installed skills found")
        return 1

    print(f"Found {len(assignments)} skill(s).")
    print("For each, type a comma-separated list of stack names (e.g. 'engineering, data'),")
    print("or just Enter to skip (no assignment).")
    print("Type 'never' to mark as never-publish.")
    print("Type 'q' to quit.\\n")

    changed = 0
    for skill_md, current in sorted(assignments.items()):
        name = skill_md.parent.name
        cur_str = ", ".join(current) if current else "(none)"
        print(f"{name}  [current: {cur_str}]")
        resp = input("  stacks > ").strip()
        if resp.lower() == "q":
            break
        if not resp:
            continue
        if resp.lower() == "never":
            # Set never_publish: true. set_skill_stacks doesn't handle this — bypass.
            text = skill_md.read_text(encoding="utf-8")
            import re as _re
            text2 = _re.sub(r"^never_publish:.*\\n", "", text, count=1, flags=_re.MULTILINE)
            text2 = _re.sub(
                r"(^---\\s*\\n)",
                r"\\1never_publish: true\\n",
                text2,
                count=1,
                flags=_re.MULTILINE,
            )
            skill_md.write_text(text2, encoding="utf-8")
            print(f"  marked never_publish")
            changed += 1
            continue
        stack_names = [s.strip() for s in resp.split(",") if s.strip()]
        if set_skill_stacks(skill_md, stack_names):
            print(f"  set stacks: {stack_names}")
            changed += 1

    print(f"\\n{changed} skill(s) updated. Run `forge stack list` to see the result.")
    return 0
'''


# CLI subparser additions
MAIN_PY_ADDITIONS = """
    stack_p = sub.add_parser("stack", help="Group skills into team-scoped stack repos")
    stack_sub = stack_p.add_subparsers(dest="stack_cmd")
    stack_sub.add_parser("list", help="List discovered stacks and their members")
    stack_diff_p = stack_sub.add_parser("diff", help="Show what publish would do")
    stack_diff_p.add_argument("name", help="Stack name")
    stack_pub_p = stack_sub.add_parser("publish", help="Generate stack repo, commit, push")
    stack_pub_p.add_argument("name", nargs="?", help="Stack name (omit if using --all)")
    stack_pub_p.add_argument("--all", action="store_true", help="Publish every stack")
    stack_sub.add_parser("assign", help="Interactive: assign existing skills to stacks")
    stack_p.set_defaults(fn=cmd_stack)
"""


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge")
        return 1

    # Step 1: write forge/stacks.py
    stacks_path = forge_dir / "stacks.py"
    if stacks_path.exists() and "def discover_stacks" in stacks_path.read_text():
        print("  + forge/stacks.py already exists (overwriting with latest)")
    stacks_path.write_text(STACKS_PY)
    print("  + wrote forge/stacks.py")

    # Step 2: add cmd_stack to commands.py
    commands_path = forge_dir / "commands.py"
    cmds = commands_path.read_text()
    if "def cmd_stack(" in cmds:
        print("  + cmd_stack already in commands.py (skipping addition)")
    else:
        cmds = cmds.rstrip() + "\n" + COMMANDS_PY_ADDITIONS
        commands_path.write_text(cmds)
        print("  + added cmd_stack to commands.py")

    # Step 3: wire cmd_stack into __main__.py
    main_path = forge_dir / "__main__.py"
    main_src = main_path.read_text()

    if "cmd_stack" not in main_src:
        # Add to the import block. The captured body ends with `cmd_tag,` (trailing
        # comma included). We need to add a new line WITH leading whitespace but
        # without an extra comma at the start.
        import_re = re.compile(r"(from \.commands import \(\n)(.*?)(\n\))", re.DOTALL)
        m = import_re.search(main_src)
        if m:
            body = m.group(2)
            # Ensure body ends with a comma (it does in normal-formatted imports)
            if not body.rstrip().endswith(","):
                body = body.rstrip() + ","
            new_body = body + "\n    cmd_stack,"
            new_import = m.group(1) + new_body + m.group(3)
            main_src = main_src[:m.start()] + new_import + main_src[m.end():]
            print("  + added cmd_stack to __main__.py imports")

        # Add the subparser. Insert before `return p` in build_parser.
        marker = "    return p"
        if marker in main_src and "stack_p = sub.add_parser" not in main_src:
            main_src = main_src.replace(marker, MAIN_PY_ADDITIONS + "\n" + marker, 1)
            print("  + added stack subparsers to build_parser")
        main_path.write_text(main_src)
    else:
        print("  + __main__.py already has cmd_stack wiring (skipping)")

    # Clear .pyc cache
    pycache = Path("forge/__pycache__")
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    # Step 4: append STACK_REPO_* hints to .env.example
    env_example = Path(".env.example")
    if env_example.exists():
        e = env_example.read_text()
        if "STACK_REPO_" not in e:
            with env_example.open("a") as f:
                f.write("\n# --- Stacks ---\n")
                f.write("# Per-stack target repo URLs. Empty/missing = local-only generation.\n")
                f.write("STACK_REPO_engineering=\n")
                f.write("STACK_REPO_data=\n")
                f.write("STACK_REPO_operations=\n")
            print("  + added STACK_REPO_* placeholders to .env.example")

    # Parse-check
    try:
        ast.parse(stacks_path.read_text())
        ast.parse(commands_path.read_text())
        ast.parse(main_path.read_text())
        print("  + all files parse cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    print()
    print("+ done")
    print()
    print("Try:")
    print("  python3 -m forge stack list           # (will be empty until you assign)")
    print("  python3 -m forge stack assign         # interactive: assign skills to stacks")
    print("  python3 -m forge stack list           # see what got assigned")
    print("  python3 -m forge stack diff engineering")
    print("  python3 -m forge stack publish engineering")
    print()
    print("For pushing to remote repos, add to .env:")
    print("  STACK_REPO_engineering=git@github.com:eli-walkingshaw/torus-skills-engineering.git")
    return 0


if __name__ == "__main__":
    sys.exit(main())
