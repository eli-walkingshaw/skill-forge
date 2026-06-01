#!/usr/bin/env bash
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
step()  { printf "${BLUE}${BOLD}==>${NC}${BOLD} %s${NC}\n" "$*"; }
ok()    { printf "  ${GREEN}✓${NC} %s\n" "$*"; }
warn()  { printf "  ${YELLOW}!${NC} %s\n" "$*"; }
fail()  { printf "  ${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

step "Checking environment"
[[ -f "forge/__main__.py" ]] || fail "Run from inside skill-forge/"
command -v python3 >/dev/null || fail "python3 not found"
command -v git >/dev/null || fail "git not found"
ok "python3 $(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ok "git ok"

VAULT_PATH="$HOME/ObsidianVaults/skill-forge"
SKILLS_REPO_PATH="$HOME/code/claude-skills"
CLAUDE_LOGS_PATH="$HOME/.claude/projects"
CLAUDE_SKILLS_LINK="$HOME/.claude/skills/user"

step "Creating directories"
mkdir -p "$VAULT_PATH" "$SKILLS_REPO_PATH" "$(dirname "$CLAUDE_SKILLS_LINK")"
ok "vault: $VAULT_PATH"
ok "skills repo: $SKILLS_REPO_PATH"

step "Git init in skills repo"
if [[ ! -d "$SKILLS_REPO_PATH/.git" ]]; then
  ( cd "$SKILLS_REPO_PATH" && git init -q -b main )
fi
if [[ ! -f "$SKILLS_REPO_PATH/README.md" ]]; then
  echo "# claude-skills" > "$SKILLS_REPO_PATH/README.md"
  ( cd "$SKILLS_REPO_PATH" && git add README.md && git commit -q -m "init" 2>/dev/null || true )
fi
if ! git -C "$SKILLS_REPO_PATH" config user.email >/dev/null 2>&1; then
  if [[ -z "$(git config --global user.email || echo)" ]]; then
    git -C "$SKILLS_REPO_PATH" config user.email "skill-forge@local"
    git -C "$SKILLS_REPO_PATH" config user.name "skill-forge"
  fi
fi
ok "git ready"

step "Symlink into Claude Code"
if [[ -L "$CLAUDE_SKILLS_LINK" ]]; then
  ok "symlink exists"
elif [[ -e "$CLAUDE_SKILLS_LINK" ]]; then
  warn "$CLAUDE_SKILLS_LINK exists (not a symlink), leaving it"
else
  ln -s "$SKILLS_REPO_PATH" "$CLAUDE_SKILLS_LINK"
  ok "linked $CLAUDE_SKILLS_LINK"
fi

step "Writing .env"
EXISTING_KEY=""
[[ -f .env ]] && EXISTING_KEY=$(grep -E '^ANTHROPIC_API_KEY=' .env | head -1 | cut -d= -f2-)
if [[ -z "$EXISTING_KEY" ]] || [[ "$EXISTING_KEY" == "sk-ant-..." ]]; then
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && EXISTING_KEY="$ANTHROPIC_API_KEY"
fi
if [[ -z "$EXISTING_KEY" ]] || [[ "$EXISTING_KEY" == "sk-ant-..." ]]; then
  printf "${YELLOW}?${NC} Paste your Anthropic API key (or Enter to skip): "
  read -r EXISTING_KEY
  [[ -z "$EXISTING_KEY" ]] && EXISTING_KEY="sk-ant-PLACEHOLDER-EDIT-DOT-ENV"
fi

cat > .env <<EOF
ANTHROPIC_API_KEY=$EXISTING_KEY
VAULT_PATH=$VAULT_PATH
SKILLS_REPO_PATH=$SKILLS_REPO_PATH
SOURCES=claude-code,inbox
CLAUDE_CODE_LOGS_PATH=$CLAUDE_LOGS_PATH
SCAN_DAYS_BACK=7
CLUSTER_MIN_SIZE=3
CLUSTER_SIMILARITY_THRESHOLD=0.45
DRAFT_MODEL=claude-opus-4-7
GIT_AUTO_PUSH=false
GIT_REMOTE=origin
GIT_BRANCH=main
EOF
chmod 600 .env
ok "wrote .env"

step "forge init"
python3 -m forge init

step "forge status"
python3 -m forge status

step "Seed note"
SEED="$VAULT_PATH/inbox/welcome.md"
if [[ ! -f "$SEED" ]]; then
  cat > "$SEED" <<'EOF'
---
goal: Welcome note - delete me later
tools: skill-forge
---
Placeholder. Drop real notes here describing patterns you want skills for.
EOF
  ok "wrote $SEED"
fi

echo ""
step "Done"
echo "Open $VAULT_PATH in Obsidian (File → Open vault → Open folder as vault)"
echo "Then: python3 -m forge scan && python3 -m forge status"
