# skill-forge

A self-updating Claude skill builder. Watches your work, notices what you keep doing, drafts SKILL.md files for you to approve in Obsidian, and syncs the approved ones to where Claude reads skills.

## How it works

```
sources (Claude Code logs, claude.ai exports, manual notes)
    ↓
forge scan       → reads sources, extracts task entries → captures.jsonl
forge cluster    → groups similar tasks (threshold: 3+) → clusters.json
forge draft      → calls Claude API per cluster → vault/proposals/*.md
                     ↓
              you review in Obsidian
                     ↓
              drag proposal → vault/approved/
                     ↓
forge watch (daemon) → validates, commits, pushes
                     ↓
              git pull on the Claude side → live
```

## Quick start

```bash
# 1. Configure
cp .env.example .env
# edit .env: VAULT_PATH, SKILLS_REPO_PATH, ANTHROPIC_API_KEY

# 2. Initialize the vault (one time)
forge init

# 3. Run the loop manually first
forge run

# 4. Once happy, schedule it
forge install-cron      # adds a nightly job
forge watch &           # background daemon for vault → git sync
```

## Commands

| Command | What it does |
|---|---|
| `forge init` | Sets up the Obsidian vault + Git repo skeleton |
| `forge scan` | Pulls task entries from configured sources into `~/.skill-forge/captures.jsonl` |
| `forge cluster` | Re-clusters all captures, writes `~/.skill-forge/clusters.json` |
| `forge draft [cluster-id]` | Drafts SKILL.md for one cluster (or all new ones) |
| `forge run` | scan → cluster → draft, all in one (cron target) |
| `forge watch` | Daemon: file-watches `approved/` and syncs to Git |
| `forge status` | Shows what's in the pipeline |
| `forge install-cron` | Adds a nightly `forge run` to crontab |

## Sources

Configured in `.env`:

- **Claude Code sessions**: point `CLAUDE_CODE_LOGS_PATH` at `~/.claude/projects/`. Forge will scan session JSONL files for user messages + Claude's responses.
- **Manual inbox**: drop any `.md` file into `vault/inbox/` describing what you just did. Forge picks it up on the next scan.
- **claude.ai exports**: export a conversation as JSON via claude.ai settings, drop it in `vault/inbox/`.

## What gets clustered

The clustering looks for *repeated patterns*, not just topics. A task entry includes:
- The high-level goal ("fix Suitelet white screen")
- The actual fix or pattern ("SVG data URI percent-encoding")
- Tools/files touched

Clustering uses TF-IDF + cosine similarity. Three or more captures with similarity above the threshold form a cluster. Tunable in `.env`.

## Why a review step

The approved/ folder is the gate. Forge proposes; you decide. Bad clusters (one-off problems, things that aren't really skills) get dragged to archive/ instead, and forge learns to avoid similar drafts next time.
