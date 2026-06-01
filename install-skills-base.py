#!/usr/bin/env python3
"""Install Skills.base into ~/Downloads/skill-forge/ for an Obsidian Bases view.

Creates a single `.base` file (Obsidian's new file type from mid-2025) that
gives you four database-like views of your skill library:

  - "All skills" — every SKILL.md in the vault as a sortable table
  - "By stack" — grouped by the `stacks` frontmatter field
  - "Imports" — filtered to skills with `forge_source:` (Oracle imports)
  - "Cards" — visual card layout for browsing

The filter keys on file.path starting with "skills/" so it only matches the
actual SKILL.md files (via the symlinked ~/code/torus-skills/ folder), not
lifecycle files in archive/, rejected/, pending/, etc.

Idempotent — re-running overwrites the .base file with the latest version.

Run from inside ~/code/skill-forge:
    python3 install-skills-base.py
"""
import sys
from pathlib import Path


# Note: YAML inside Python string — be careful with quoting.
# Bases filter operators: ==, !=, contains, startsWith, hasLink, hasTag, etc.
# A skill file is identifiable by being inside skills/ (the symlink) AND
# named SKILL.md.
BASE_CONTENT = """filters:
  and:
    - 'file.path.startsWith("skills/")'
    - 'file.name == "SKILL.md"'
formulas:
  skill_name: 'file.folder.split("/").last()'
  stack_path: 'file.folder.split("/")[1]'
  related_count: 'if(related, related.length, 0)'
  source_kind: 'if(forge_source, "imported", "captured")'
properties:
  formula.skill_name:
    displayName: Skill
  formula.stack_path:
    displayName: Stack
  note.description:
    displayName: Description
  formula.related_count:
    displayName: Links
  formula.source_kind:
    displayName: Source
  note.stacks:
    displayName: Stacks
  note.forge_source:
    displayName: Origin
views:
  - type: table
    name: "All skills"
    order:
      - formula.skill_name
      - formula.stack_path
      - note.description
      - formula.related_count
      - formula.source_kind

  - type: table
    name: "By stack"
    groupBy:
      property: formula.stack_path
      direction: ASC
    order:
      - formula.skill_name
      - note.description
      - formula.related_count

  - type: table
    name: "Imports only"
    filters:
      and:
        - 'forge_source != null'
    order:
      - formula.skill_name
      - formula.stack_path
      - note.forge_source

  - type: cards
    name: "Cards"
    order:
      - formula.skill_name
      - note.description
"""


def main() -> int:
    # Load .env to find VAULT_PATH
    env_vars = {}
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()
    vault_path_str = env_vars.get("VAULT_PATH")
    if not vault_path_str:
        print("X VAULT_PATH not in .env — run from ~/code/skill-forge")
        return 1
    vault = Path(vault_path_str).expanduser()
    if not vault.exists():
        print(f"X vault path {vault} doesn't exist")
        return 1

    # Verify the skills symlink exists
    skills_link = vault / "skills"
    if not skills_link.exists():
        print(f"  ! warning: {skills_link} doesn't exist")
        print("    Run this first to make skills visible:")
        print(f"    ln -s ~/code/torus-skills {skills_link}")
        # Continue anyway; .base file is still useful, just empty until link exists

    # Write the .base file
    base_path = vault / "Skills.base"
    base_path.write_text(BASE_CONTENT, encoding="utf-8")
    print(f"  + wrote {base_path}")
    print()
    print("+ done")
    print()
    print("To open in Obsidian:")
    print("  1. Reload the vault: Cmd+R")
    print("  2. In the file tree (left sidebar), open Skills.base")
    print("  3. You should see four views as tabs at the top:")
    print("       All skills | By stack | Imports only | Cards")
    print()
    print("If Bases isn't available in your Obsidian version, enable the core")
    print("plugin: Settings → Core plugins → Bases (toggle on)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
