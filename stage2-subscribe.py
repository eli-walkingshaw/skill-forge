#!/usr/bin/env python3
"""Stage 2: forge subscribe — register external skill repos.

Adds a small subsystem for tracking upstream repos you want to pull SKILL.md
files from. This stage only handles registration and git clone/pull — no
import logic yet (that's stage 3).

Adds:
  - forge/subscriptions.py — the subscription store + git wrappers
  - `forge subscribe` command tree (add/list/remove/pull/pin/unpin)
  - State at ~/.skill-forge/subscriptions.json
  - Clones at ~/.skill-forge/subscribed/<name>/

Run from inside ~/code/skill-forge:
    python3 stage2-subscribe.py
"""
import ast
import re
import sys
from pathlib import Path


SUBSCRIPTIONS_PY = '''"""Subscribe to upstream skill repos.

Each subscription is identified by a `name` (slug). Persisted state lives at
~/.skill-forge/subscriptions.json. Clones live at ~/.skill-forge/subscribed/<name>/.

A subscription tracks:
  - url: git remote URL
  - branch: branch to track (default: main)
  - filter: glob pattern for relevant files (default: **/SKILL.md)
  - pinned_sha: if non-empty, freeze at this SHA instead of tracking branch
  - last_pulled_sha: most recent SHA after a pull (informational)
  - last_pulled_at: ISO-8601 timestamp of last pull
"""
from __future__ import annotations
import datetime as _dt
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from pathlib import Path


SUBSCRIPTIONS_DIR = Path.home() / ".skill-forge"
SUBSCRIPTIONS_FILE = SUBSCRIPTIONS_DIR / "subscriptions.json"
CLONES_DIR = SUBSCRIPTIONS_DIR / "subscribed"

VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


@dataclass
class Subscription:
    name: str
    url: str
    branch: str = "main"
    filter: str = "**/SKILL.md"
    pinned_sha: str = ""
    last_pulled_sha: str = ""
    last_pulled_at: str = ""

    @property
    def clone_path(self) -> Path:
        return CLONES_DIR / self.name

    def to_dict(self) -> dict:
        return asdict(self)


def _ensure_state_dirs() -> None:
    SUBSCRIPTIONS_DIR.mkdir(parents=True, exist_ok=True)
    CLONES_DIR.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict:
    _ensure_state_dirs()
    if not SUBSCRIPTIONS_FILE.exists():
        return {"subscriptions": []}
    try:
        return json.loads(SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Don't silently wipe on bad state — surface to caller
        raise RuntimeError(
            f"subscriptions.json is corrupt: {SUBSCRIPTIONS_FILE}. "
            "Fix manually or remove and re-add subscriptions."
        )


def _save_state(state: dict) -> None:
    _ensure_state_dirs()
    SUBSCRIPTIONS_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )


def list_subscriptions() -> list[Subscription]:
    state = _load_state()
    return [Subscription(**entry) for entry in state.get("subscriptions", [])]


def get_subscription(name: str) -> Subscription | None:
    for sub in list_subscriptions():
        if sub.name == name:
            return sub
    return None


def _write_subscriptions(subs: list[Subscription]) -> None:
    _save_state({"subscriptions": [s.to_dict() for s in subs]})


def add_subscription(
    name: str,
    url: str,
    branch: str = "main",
    filter_pattern: str = "**/SKILL.md",
) -> Subscription:
    """Register and clone a new subscription. Raises if name exists or invalid."""
    if not VALID_NAME_RE.match(name):
        raise ValueError(
            f"name '{name}' invalid — must be lowercase kebab-case, "
            "start with a letter, max 41 chars"
        )
    if get_subscription(name):
        raise ValueError(f"subscription '{name}' already exists")

    sub = Subscription(name=name, url=url, branch=branch, filter=filter_pattern)
    _ensure_state_dirs()
    if sub.clone_path.exists():
        raise ValueError(
            f"clone path {sub.clone_path} already exists — "
            "remove it first or pick a different name"
        )

    # Clone shallow to save space; we don't need history for skill files
    cmd = ["git", "clone", "--depth", "1", "--branch", branch, url, str(sub.clone_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed:\\n{result.stderr.strip()}")

    sub.last_pulled_sha = _current_sha(sub.clone_path)
    sub.last_pulled_at = _now_iso()

    subs = list_subscriptions()
    subs.append(sub)
    _write_subscriptions(subs)
    return sub


def remove_subscription(name: str) -> bool:
    """Remove subscription + delete clone. Returns True if anything was removed."""
    subs = list_subscriptions()
    target = next((s for s in subs if s.name == name), None)
    if not target:
        return False
    # Delete clone
    if target.clone_path.exists():
        shutil.rmtree(target.clone_path)
    # Drop from state
    new_subs = [s for s in subs if s.name != name]
    _write_subscriptions(new_subs)
    return True


def pull_subscription(name: str) -> tuple[bool, str]:
    """Pull latest from the subscription's branch (or noop if pinned).

    Returns (changed, message). 'changed' is True if new commits were pulled.
    """
    sub = get_subscription(name)
    if not sub:
        return False, f"no subscription named '{name}'"
    if not sub.clone_path.exists():
        return False, f"clone missing at {sub.clone_path} — try re-adding"

    if sub.pinned_sha:
        # Pinned: do nothing
        current = _current_sha(sub.clone_path)
        if current == sub.pinned_sha:
            return False, f"pinned at {sub.pinned_sha[:8]} (no change)"
        # Otherwise reset to the pinned SHA (in case the clone drifted)
        result = subprocess.run(
            ["git", "-C", str(sub.clone_path), "fetch", "origin"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"fetch failed: {result.stderr.strip()}"
        result = subprocess.run(
            ["git", "-C", str(sub.clone_path), "reset", "--hard", sub.pinned_sha],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"reset to pinned SHA failed: {result.stderr.strip()}"
        return True, f"reset to pinned SHA {sub.pinned_sha[:8]}"

    # Track branch — git pull
    prev_sha = _current_sha(sub.clone_path)
    result = subprocess.run(
        ["git", "-C", str(sub.clone_path), "pull", "--ff-only", "origin", sub.branch],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, f"pull failed: {result.stderr.strip()}"
    new_sha = _current_sha(sub.clone_path)

    # Update state
    sub.last_pulled_sha = new_sha
    sub.last_pulled_at = _now_iso()
    subs = list_subscriptions()
    subs = [s if s.name != name else sub for s in subs]
    _write_subscriptions(subs)

    if prev_sha == new_sha:
        return False, f"already at {new_sha[:8]} (no change)"
    return True, f"{prev_sha[:8]} -> {new_sha[:8]}"


def pin_subscription(name: str, sha: str) -> None:
    """Freeze a subscription at a specific SHA."""
    if not re.match(r"^[a-f0-9]{7,40}$", sha):
        raise ValueError(f"sha '{sha}' looks invalid — expected 7-40 hex chars")
    sub = get_subscription(name)
    if not sub:
        raise ValueError(f"no subscription named '{name}'")
    if not sub.clone_path.exists():
        raise ValueError(f"clone missing at {sub.clone_path}")

    # Make sure the SHA exists in the clone (fetch first)
    result = subprocess.run(
        ["git", "-C", str(sub.clone_path), "fetch", "origin"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"fetch failed: {result.stderr.strip()}")

    # Verify SHA exists
    result = subprocess.run(
        ["git", "-C", str(sub.clone_path), "cat-file", "-e", sha + "^{commit}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sha '{sha}' not found in clone after fetch")

    # Reset to it
    result = subprocess.run(
        ["git", "-C", str(sub.clone_path), "reset", "--hard", sha],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reset failed: {result.stderr.strip()}")

    full_sha = _current_sha(sub.clone_path)
    sub.pinned_sha = full_sha
    sub.last_pulled_sha = full_sha
    sub.last_pulled_at = _now_iso()

    subs = list_subscriptions()
    subs = [s if s.name != name else sub for s in subs]
    _write_subscriptions(subs)


def unpin_subscription(name: str) -> None:
    """Remove the SHA pin so subscription tracks branch again."""
    sub = get_subscription(name)
    if not sub:
        raise ValueError(f"no subscription named '{name}'")
    sub.pinned_sha = ""
    subs = list_subscriptions()
    subs = [s if s.name != name else sub for s in subs]
    _write_subscriptions(subs)


def _current_sha(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
'''


COMMANDS_PY_ADDITIONS = '''

# ---------- forge subscribe ------------------------------------------------


def cmd_subscribe(args, config: Config) -> int:
    from .subscriptions import (
        add_subscription,
        get_subscription,
        list_subscriptions,
        pin_subscription,
        pull_subscription,
        remove_subscription,
        unpin_subscription,
    )

    sub = args.subscribe_cmd or "list"

    if sub == "list":
        subs = list_subscriptions()
        if not subs:
            print("(no subscriptions — use `forge subscribe add <name> <url>`)")
            return 0
        print(f"{len(subs)} subscription(s):\\n")
        for s in subs:
            pin_note = f" [PINNED at {s.pinned_sha[:8]}]" if s.pinned_sha else ""
            print(f"  {s.name}{pin_note}")
            print(f"    url:        {s.url}")
            print(f"    branch:     {s.branch}")
            print(f"    filter:     {s.filter}")
            print(f"    clone:      {s.clone_path}")
            last = s.last_pulled_sha[:8] if s.last_pulled_sha else "(never)"
            when = s.last_pulled_at or "(never)"
            print(f"    last pull:  {last} at {when}")
            print()
        return 0

    if sub == "add":
        if not args.name or not args.url:
            print("usage: forge subscribe add <name> <url> [--branch B] [--filter G]")
            return 1
        try:
            s = add_subscription(
                args.name,
                args.url,
                branch=args.branch or "main",
                filter_pattern=args.filter or "**/SKILL.md",
            )
        except (ValueError, RuntimeError) as e:
            print(f"X {e}")
            return 1
        print(f"+ subscribed: {s.name}")
        print(f"  cloned to:  {s.clone_path}")
        print(f"  at SHA:     {s.last_pulled_sha[:8]}")
        return 0

    if sub == "remove":
        if not args.name:
            print("usage: forge subscribe remove <name>")
            return 1
        if remove_subscription(args.name):
            print(f"+ removed: {args.name}")
            return 0
        print(f"no subscription named '{args.name}'")
        return 1

    if sub == "pull":
        targets: list = []
        if args.all:
            targets = list_subscriptions()
        elif args.name:
            s = get_subscription(args.name)
            if not s:
                print(f"no subscription named '{args.name}'")
                return 1
            targets = [s]
        else:
            print("usage: forge subscribe pull <name> | --all")
            return 1

        any_changed = False
        for s in targets:
            changed, msg = pull_subscription(s.name)
            marker = "*" if changed else " "
            print(f"  {marker} {s.name}: {msg}")
            if changed:
                any_changed = True
        if not any_changed:
            print("\\n(no subscriptions had updates)")
        return 0

    if sub == "pin":
        if not args.name or not args.sha:
            print("usage: forge subscribe pin <name> <sha>")
            return 1
        try:
            pin_subscription(args.name, args.sha)
        except (ValueError, RuntimeError) as e:
            print(f"X {e}")
            return 1
        print(f"+ pinned {args.name} at {args.sha}")
        return 0

    if sub == "unpin":
        if not args.name:
            print("usage: forge subscribe unpin <name>")
            return 1
        try:
            unpin_subscription(args.name)
        except ValueError as e:
            print(f"X {e}")
            return 1
        print(f"+ unpinned {args.name} — now tracking branch")
        return 0

    print(f"unknown subscribe subcommand: {sub}")
    return 1
'''


MAIN_PY_ADDITIONS = """
    sub_p = sub.add_parser("subscribe", help="Register upstream skill repos")
    subscribe_sub = sub_p.add_subparsers(dest="subscribe_cmd")
    subscribe_sub.add_parser("list", help="List all subscriptions and their state")
    add_p = subscribe_sub.add_parser("add", help="Add a subscription")
    add_p.add_argument("name", help="Slug-name for this subscription")
    add_p.add_argument("url", help="Git URL of the upstream repo")
    add_p.add_argument("--branch", help="Branch to track (default: main)")
    add_p.add_argument("--filter", help="Glob for files to pull (default: **/SKILL.md)")
    pull_p = subscribe_sub.add_parser("pull", help="Pull latest from upstream(s)")
    pull_p.add_argument("name", nargs="?", help="Subscription name")
    pull_p.add_argument("--all", action="store_true", help="Pull all subscriptions")
    rm_p = subscribe_sub.add_parser("remove", help="Unsubscribe + delete clone")
    rm_p.add_argument("name", help="Subscription name")
    pin_p = subscribe_sub.add_parser("pin", help="Freeze at a specific SHA")
    pin_p.add_argument("name", help="Subscription name")
    pin_p.add_argument("sha", help="Full or short SHA")
    unpin_p = subscribe_sub.add_parser("unpin", help="Resume branch tracking")
    unpin_p.add_argument("name", help="Subscription name")
    sub_p.set_defaults(fn=cmd_subscribe)
"""


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge")
        return 1

    # Step 1: write forge/subscriptions.py
    subs_path = forge_dir / "subscriptions.py"
    if subs_path.exists() and "def add_subscription" in subs_path.read_text():
        print("  + forge/subscriptions.py already exists (overwriting with latest)")
    subs_path.write_text(SUBSCRIPTIONS_PY)
    print("  + wrote forge/subscriptions.py")

    # Step 2: add cmd_subscribe to commands.py
    commands_path = forge_dir / "commands.py"
    cmds = commands_path.read_text()
    if "def cmd_subscribe(" in cmds:
        print("  + cmd_subscribe already in commands.py (skipping)")
    else:
        cmds = cmds.rstrip() + "\n" + COMMANDS_PY_ADDITIONS
        commands_path.write_text(cmds)
        print("  + added cmd_subscribe to commands.py")

    # Step 3: wire cmd_subscribe into __main__.py
    main_path = forge_dir / "__main__.py"
    main_src = main_path.read_text()

    if "cmd_subscribe" not in main_src:
        # Add to imports
        import_re = re.compile(r"(from \.commands import \(\n)(.*?)(\n\))", re.DOTALL)
        m = import_re.search(main_src)
        if m:
            body = m.group(2)
            if not body.rstrip().endswith(","):
                body = body.rstrip() + ","
            new_body = body + "\n    cmd_subscribe,"
            new_import = m.group(1) + new_body + m.group(3)
            main_src = main_src[:m.start()] + new_import + main_src[m.end():]
            print("  + added cmd_subscribe to __main__.py imports")

        # Add subparsers before `return p`
        marker = "    return p"
        if marker in main_src and 'sub_p = sub.add_parser("subscribe"' not in main_src:
            main_src = main_src.replace(marker, MAIN_PY_ADDITIONS + "\n" + marker, 1)
            print("  + added subscribe subparsers to build_parser")
        main_path.write_text(main_src)
    else:
        print("  + __main__.py already has cmd_subscribe wiring (skipping)")

    # Clear .pyc cache
    pycache = forge_dir / "__pycache__"
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    # Parse-check
    try:
        ast.parse(subs_path.read_text())
        ast.parse(commands_path.read_text())
        ast.parse(main_path.read_text())
        print("  + all files parse cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    print()
    print("+ stage 2 complete")
    print()
    print("Try:")
    print("  python3 -m forge subscribe list                       # (empty until you add one)")
    print("  python3 -m forge subscribe add anthropic-skills https://github.com/anthropic/skills.git")
    print("  python3 -m forge subscribe list                       # see it registered")
    print("  python3 -m forge subscribe pull anthropic-skills      # check for updates")
    print("  python3 -m forge subscribe pin anthropic-skills <sha> # freeze")
    print("  python3 -m forge subscribe remove anthropic-skills    # unregister")
    return 0


if __name__ == "__main__":
    sys.exit(main())
