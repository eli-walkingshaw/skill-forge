# skill-forge

A workshop for Claude skills. Drafts, edits, and audits SKILL.md files; reviews live in Obsidian; approved skills auto-install into your skills repo and sync to Git.

## Commands

| Command | What it does |
|---|---|
| `forge new <name>` | Drop a blank SKILL.md template into `proposals/` for you to fill in |
| `forge draft "<description>"` | Claude drafts a SKILL.md from a one-line description, using your existing skills as style reference |
| `forge edit <name>` | Pull an installed skill into `proposals/` so you can edit it safely |
| `forge inbox-to-skill <note>` | Promote an inbox note into a drafted SKILL.md |
| `forge audit` | Claude reviews your skills for gaps, staleness, duplicates, weak descriptions |
| `forge list` | Show all installed skills |
| `forge status` | Pipeline state (proposals awaiting review, etc.) |
| `forge watch` | Daemon: when you drag a proposal to `approved/`, validate + install + commit |
| `forge init` | Create vault folders (idempotent) |

## The loop

```
forge new / draft / edit / inbox-to-skill
        ↓
   vault/proposals/  ← review in Obsidian
        ↓
   drag → vault/approved/
        ↓
   forge watch (daemon)
        ↓
   skills-repo/<name>/SKILL.md  ← committed
        ↓
   symlinked into ~/.claude/skills/user
        ↓
   Claude Code sees it
```

## Quick start

```bash
cp .env.example .env
# Edit .env — at minimum: ANTHROPIC_API_KEY, VAULT_PATH, SKILLS_REPO_PATH
python3 -m forge init
python3 -m forge list           # see what's already installed
python3 -m forge draft "handle the NetSuite SuiteQL column-name traps"
# review the proposal in Obsidian, then drag it to approved/
python3 -m forge watch &        # background daemon — installs anything approved
```

## Why this shape

Most skill-authoring is iterative: you write a draft, refine the description, test the trigger, edit again. Pushing that loop through Obsidian (instead of editing files in the skills repo directly) gives you:

- A safe review surface — the draft isn't live until you move it
- Git hygiene — every approved skill is a clean commit
- A natural inbox for half-formed pattern observations that may or may not graduate to skills

The earlier version of this tool tried to auto-detect patterns from Claude Code session logs and cluster them into skill candidates. That worked technically but produced low-signal results, because session logs are mostly help-seeking ("X is broken, help") not pattern-stating ("I solved X by doing Y"). The current version drops auto-detection and assumes you know when a skill is needed — you just want low-friction tools to write it.

