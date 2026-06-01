#!/usr/bin/env python3
"""Fix: list_subscriptions() chokes when seen_hashes (added by stage 3) is in state file.

The Subscription dataclass doesn't know about `seen_hashes`, so calling
Subscription(**entry) raises TypeError when stage 3 has written hash records
into the state file.

Fix: filter the state entries to only fields the dataclass knows about.

Run from inside ~/code/skill-forge:
    python3 fix-subscriptions-extra-fields.py
"""
import ast
import sys
from pathlib import Path


def main() -> int:
    subs_path = Path("forge/subscriptions.py")
    if not subs_path.exists():
        print("X forge/subscriptions.py not found — run from ~/code/skill-forge")
        return 1

    s = subs_path.read_text()

    if "known_fields = set(Subscription.__dataclass_fields__.keys())" in s:
        print("  + already patched")
        return 0

    old = """def list_subscriptions() -> list[Subscription]:
    state = _load_state()
    return [Subscription(**entry) for entry in state.get("subscriptions", [])]"""

    new = """def list_subscriptions() -> list[Subscription]:
    state = _load_state()
    # Filter to only the fields the Subscription dataclass knows about. This
    # lets the state file carry extra keys (like seen_hashes managed by sync)
    # without breaking the dataclass init.
    known_fields = set(Subscription.__dataclass_fields__.keys())
    out = []
    for entry in state.get("subscriptions", []):
        filtered = {k: v for k, v in entry.items() if k in known_fields}
        out.append(Subscription(**filtered))
    return out"""

    if old not in s:
        print("X expected shape of list_subscriptions not found — bailing")
        return 1

    s = s.replace(old, new, 1)
    subs_path.write_text(s)

    try:
        ast.parse(s)
        print("  + patched and parses cleanly")
    except SyntaxError as e:
        print(f"X syntax error: {e}")
        return 1

    pycache = Path("forge/__pycache__")
    if pycache.exists():
        for pyc in pycache.glob("*.pyc"):
            pyc.unlink()
        print("  + cleared .pyc cache")

    return 0


if __name__ == "__main__":
    sys.exit(main())
