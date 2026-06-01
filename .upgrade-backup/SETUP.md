# Setup walkthrough

## 1. Install

```bash
# Clone or copy the skill-forge folder somewhere stable, e.g. ~/code/skill-forge
cd ~/code/skill-forge

# It uses only the Python stdlib — Python 3.10+ is enough.
python3 -m forge --help
```

## 2. Create the Obsidian vault

Skill-forge wants a vault dedicated to itself (you said separate vault, so this is clean).

```bash
mkdir -p ~/ObsidianVaults/skill-forge
```

Then in Obsidian: **File → Open vault → Open folder as vault** → pick `~/ObsidianVaults/skill-forge`.

## 3. Create the skills repo

This is the Git repo Claude Code will read from.

```bash
mkdir -p ~/code/claude-skills
cd ~/code/claude-skills
git init -b main
gh repo create claude-skills --private --source=. --remote=origin  # or use github.com UI
echo "# claude-skills" > README.md
git add . && git commit -m "init" && git push -u origin main
```

You can use any host (GitHub, Gitea, self-hosted) — forge only calls `git push` against whatever remote you set.

## 4. Point Claude Code at the repo

So that approved skills become live skills for your future Claude Code sessions:

```bash
# Option A: symlink the repo into Claude Code's user skills directory
mkdir -p ~/.claude/skills
ln -s ~/code/claude-skills ~/.claude/skills/user

# Option B: clone the repo directly into the skills directory and `git pull` periodically
git clone <your-repo-url> ~/.claude/skills/user
```

Option A is simpler — one source of truth on disk.

## 5. Configure

```bash
cd ~/code/skill-forge
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY, VAULT_PATH, SKILLS_REPO_PATH
```

## 6. Initialize

```bash
python3 -m forge init
```

This creates `inbox/`, `proposals/`, `approved/`, `archive/` inside your vault.

## 7. Smoke test it

Drop a note in the inbox to verify the loop works without waiting for natural data:

```bash
cat > ~/ObsidianVaults/skill-forge/inbox/test.md <<'EOF'
---
goal: test that forge works
tools: skill-forge
---
This is just a placeholder note to verify the pipeline. Won't form a cluster
on its own (need 3+) but `forge scan` and `forge status` should pick it up.
EOF

python3 -m forge scan
python3 -m forge status
```

You should see "captures stored: 1".

## 8. The real workflow

Once you have ~10+ Claude Code sessions or inbox notes:

```bash
python3 -m forge run    # scan + cluster + draft
```

Open Obsidian. Look in `proposals/`. Each file is a draft SKILL.md with a callout block on top explaining what cluster it came from.

**To approve a proposal**: in Obsidian's file explorer, drag the proposal file from `proposals/` to `approved/`. (Or in a terminal: `mv vault/proposals/foo.md vault/approved/`.)

**To reject**: drag to `archive/` instead. Forge won't propose the same cluster again — its fingerprint is stored in `~/.skill-forge/drafted.json`.

## 9. Run the watcher

In a terminal that stays open (or via a launchd plist / systemd unit):

```bash
python3 -m forge watch
```

This sits on `approved/`, validates the SKILL.md, copies it to the skills repo, commits, and pushes. Then your next `git pull` (or symlink scenario, where it's instant) makes the skill live.

## 10. Schedule the scan/draft

```bash
python3 -m forge install-cron
```

Prints a crontab line. Run `crontab -e` and paste it. It runs `forge run` nightly at 2 AM and appends to `~/.skill-forge/cron.log`.

---

## Tuning

- **Too few clusters?** Lower `CLUSTER_SIMILARITY_THRESHOLD` (try 0.35).
- **Garbage clusters merging unrelated stuff?** Raise it (try 0.55).
- **Want skills proposed faster?** Lower `CLUSTER_MIN_SIZE` to 2 — but expect more false positives.
- **API costs adding up?** The only API calls are the draft step, once per new cluster. With min_size=3 and a nightly cron, this is usually a handful per week.

## Troubleshooting

- **`config error: Missing required env var`** — run forge from the directory containing `.env`, or copy it next to wherever you run from.
- **Watcher silently fails to push** — check `~/code/claude-skills` has a remote set: `git -C ~/code/claude-skills remote -v`. Set `GIT_AUTO_PUSH=false` in `.env` if you want to push manually.
- **Claude Code logs not picked up** — verify `CLAUDE_CODE_LOGS_PATH` exists and contains `.jsonl` files. Path varies by Claude Code version; check `~/.claude/projects/` and `~/.config/claude-code/`.
