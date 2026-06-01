# skill-forge vault

This vault is the review surface for skill-forge.

## Folders

- **`inbox/`** — drop notes about tasks. Forge reads these on each scan. Format is free, but with frontmatter:
  ```
  ---
  goal: short description of what you were doing
  tools: SuiteScript, Rhino
  ---
  Body of the note describing the fix or pattern.
  ```
  You can also drop claude.ai conversation exports (`.json`) here.

- **`proposals/`** — AI-drafted SKILL.md candidates. Each has a callout block on top explaining the cluster it came from, then the proposed SKILL.md.

- **`approved/`** — drag a proposal here to ship it. The watcher daemon picks it up, validates, commits to the skills repo, pushes, and moves the file to archive/.

- **`archive/`** — rejected proposals and successfully installed ones. Rejected proposals are remembered so forge won't re-propose the same cluster.

## Workflow

1. Forge proposes a draft in `proposals/`.
2. You open it in Obsidian, read it like any note. Edit freely — the file is yours.
3. Decide: drag to `approved/` (ship it) or `archive/` (no thanks).
4. If approved, it's live in your Claude skills within seconds.
