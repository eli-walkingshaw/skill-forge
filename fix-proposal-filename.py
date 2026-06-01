#!/usr/bin/env python3
"""Patch proposal_filename in commands.py to accept gist= keyword.

Idempotent — safe to run multiple times. Strips any duplicate definitions
of proposal_filename, _FILLER_WORDS, and _shorten_for_filename, then inserts
clean versions before the "# ---------- forge new" marker.

Run from inside ~/code/skill-forge:
    python3 fix-proposal-filename.py
"""
import re
import sys
from pathlib import Path


def main() -> int:
    commands_py = Path("forge/commands.py")
    if not commands_py.exists():
        print("✗ forge/commands.py not found — run this from ~/code/skill-forge")
        return 1

    s = commands_py.read_text()

    # Strip ALL existing definitions of these three symbols so we can
    # insert clean canonical versions in one place.
    s = _strip_top_level(s, r"^def proposal_filename\b")
    s = _strip_top_level(s, r"^_FILLER_WORDS\s*=")
    s = _strip_top_level(s, r"^def _shorten_for_filename\b")

    new_block = NEW_BLOCK

    marker = "# ---------- forge new"
    idx = s.find(marker)
    if idx == -1:
        print("✗ couldn't find 'forge new' section marker — aborting")
        return 1

    s = s[:idx] + new_block + s[idx:]
    commands_py.write_text(s)

    print("✓ patched commands.py")
    print()

    # Sanity check: find each symbol exactly once
    matches = {}
    for label, pattern in [
        ("proposal_filename", r"^def proposal_filename\b"),
        ("_FILLER_WORDS", r"^_FILLER_WORDS\s*="),
        ("_shorten_for_filename", r"^def _shorten_for_filename\b"),
    ]:
        count = sum(1 for line in s.splitlines() if re.match(pattern, line))
        matches[label] = count
        status = "✓" if count == 1 else "✗"
        print(f"  {status} {label}: {count} definition(s) (want 1)")

    if any(c != 1 for c in matches.values()):
        print()
        print("✗ unexpected count — check commands.py manually")
        return 1

    print()
    print("Now try:")
    print("  python3 -m forge capture --note \"claude skill builder\"")
    return 0


def _strip_top_level(text: str, start_re: str) -> str:
    """Strip every block matching start_re at column 0, up to the next top-level def/class/section."""
    stop_re = r"^(def |class |# ----)"
    lines = text.splitlines(keepends=True)
    out = []
    i = 0
    start_pat = re.compile(start_re)
    stop_pat = re.compile(stop_re)
    while i < len(lines):
        if start_pat.match(lines[i]):
            # Skip from here until the next top-level def/class/section marker.
            i += 1
            while i < len(lines) and not stop_pat.match(lines[i]):
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return "".join(out)


NEW_BLOCK = '''def proposal_filename(config: Config, name: str, gist: str = "") -> Path:
    """Filename = first 3-4 words of the gist (or skill name if no gist).

    Date lives inside the proposal callout header, not in the filename,
    so Obsidian's sidebar stays readable. Collisions on the same name get -2, -3.
    """
    raw = (gist or name).strip()
    short = _shorten_for_filename(raw)
    if not short:
        short = kebab(name) or "untitled"
    base = config.proposals_dir / f"{short}.md"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = config.proposals_dir / f"{short}-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


_FILLER_WORDS = {
    "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
    "to", "for", "of", "in", "on", "at", "by", "with", "from", "as",
    "that", "this", "these", "those", "it", "its", "be", "been",
    "my", "our", "your", "their", "his", "her",
    "i", "we", "they", "you",
    "how", "what", "when", "why",
    "just", "really", "very", "still",
    "fix", "fixing", "fixed",
}


def _shorten_for_filename(text: str, max_words: int = 4, max_chars: int = 32) -> str:
    """Pull a short, content-bearing kebab summary out of free text."""
    if not text:
        return ""
    text = re.sub(r"[`\\\\]+", " ", text)
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    kept = []
    seen = set()
    for w in words:
        if w in _FILLER_WORDS or len(w) < 2:
            continue
        if w in seen:
            continue
        seen.add(w)
        kept.append(w)
        if len(kept) >= max_words:
            break
    if not kept:
        kept = words[:max_words]
    result = "-".join(kept)
    return result[:max_chars].rstrip("-")


'''


if __name__ == "__main__":
    sys.exit(main())
