"""Subscribe to upstream skill repos.

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
    # Filter to only the fields the Subscription dataclass knows about. This
    # lets the state file carry extra keys (like seen_hashes managed by sync)
    # without breaking the dataclass init.
    known_fields = set(Subscription.__dataclass_fields__.keys())
    out = []
    for entry in state.get("subscriptions", []):
        filtered = {k: v for k, v in entry.items() if k in known_fields}
        out.append(Subscription(**filtered))
    return out


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
        raise RuntimeError(f"git clone failed:\n{result.stderr.strip()}")

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
