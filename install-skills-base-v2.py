#!/usr/bin/env python3
"""Overwrite the Skills.base with simpler, more reliable filter primitives.

Earlier .base file had `file.path.startsWith("skills/")` which produced an
empty result. Switching to `file.inFolder()` which is a documented Bases
function and behaves more predictably.

We also simplify formulas: instead of relying on file.folder.split() which
may behave differently across Obsidian versions, we use plain Bases-native
functions.

WARNING: this overwrites the user's Skills.base — any UI customizations
(column widths, sort orders) added through Obsidian's Bases UI will be
discarded. Save them somewhere if you care.

Run from inside ~/code/skill-forge:
    python3 install-skills-base-v2.py
"""
import sys
from pathlib import Path


BASE_CONTENT = """filters:
  and:
    - 'file.inFolder("skills")'
    - 'file.name == "SKILL.md"'
properties:
  note.description:
    displayName: Description
  note.stacks:
    displayName: Stacks
  note.related:
    displayName: Related
  note.forge_source:
    displayName: Origin
  file.folder:
    displayName: Path
views:
  - type: table
    name: "All skills"
    order:
      - file.name
      - file.folder
      - note.description

  - type: table
    name: "By stack"
    groupBy:
      property: file.folder
      direction: ASC
    order:
      - file.name
      - note.description

  - type: table
    name: "Imports only"
    filters:
      and:
        - 'forge_source != null'
    order:
      - file.name
      - file.folder
      - note.forge_source

  - type: cards
    name: "Cards"
    order:
      - file.name
      - note.description
"""


def main() -> int:
    env_vars = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
    vault_path_str = env_vars.get("VAULT_PATH")
    if not vault_path_str:
        print("X VAULT_PATH not in .env")
        return 1
    vault = Path(vault_path_str).expanduser()
    if not vault.exists():
        print(f"X vault {vault} doesn't exist")
        return 1

    base_path = vault / "Skills.base"
    if base_path.exists():
        print(f"  ! overwriting existing {base_path}")
        print("    (any UI customizations like column widths will be lost)")
    base_path.write_text(BASE_CONTENT, encoding="utf-8")
    print(f"  + wrote {base_path}")
    print()
    print("+ done")
    print()
    print("In Obsidian:")
    print("  1. Cmd+R to reload the vault")
    print("  2. Click Skills.base in the file tree")
    print("  3. You should see four tabs — All skills | By stack | Imports only | Cards")
    print()
    print("If still blank, run the diagnostic and report back:")
    print("  ls ~/Downloads/skill-forge/skills/engineering/ | head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
