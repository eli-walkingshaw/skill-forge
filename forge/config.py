"""Configuration loading from .env."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't require python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't clobber values already set in the environment.
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    vault_path: Path
    skills_repo_path: Path
    sources: list[str]
    claude_code_logs_path: Path
    scan_days_back: int
    cluster_min_size: int
    cluster_similarity_threshold: float
    draft_model: str
    git_auto_push: bool
    git_remote: str
    git_branch: str

    @property
    def inbox_dir(self) -> Path:
        return self.vault_path / "inbox"

    @property
    def proposals_dir(self) -> Path:
        return self.vault_path / "proposals"

    @property
    def approved_dir(self) -> Path:
        return self.vault_path / "approved"

    @property
    def archive_dir(self) -> Path:
        return self.vault_path / "archive"

    @property
    def state_dir(self) -> Path:
        return Path.home() / ".skill-forge"

    @property
    def captures_path(self) -> Path:
        return self.state_dir / "captures.jsonl"

    @property
    def clusters_path(self) -> Path:
        return self.state_dir / "clusters.json"

    @property
    def drafted_path(self) -> Path:
        # Tracks which cluster fingerprints we've already drafted.
        return self.state_dir / "drafted.json"


def load_config(env_file: Path | None = None) -> Config:
    """Load config from .env (next to project root) and environment."""
    if env_file is None:
        # Look in the current directory and parents for a .env.
        cwd = Path.cwd()
        for d in [cwd] + list(cwd.parents):
            candidate = d / ".env"
            if candidate.exists():
                env_file = candidate
                break

    if env_file:
        _load_dotenv(env_file)

    def _required(key: str) -> str:
        value = os.environ.get(key)
        if not value:
            raise RuntimeError(f"Missing required env var: {key}")
        return value

    return Config(
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        vault_path=Path(_required("VAULT_PATH")).expanduser(),
        skills_repo_path=Path(_required("SKILLS_REPO_PATH")).expanduser(),
        sources=[s.strip() for s in os.environ.get("SOURCES", "inbox").split(",") if s.strip()],
        claude_code_logs_path=Path(
            os.environ.get("CLAUDE_CODE_LOGS_PATH", "~/.claude/projects")
        ).expanduser(),
        scan_days_back=int(os.environ.get("SCAN_DAYS_BACK", "7")),
        cluster_min_size=int(os.environ.get("CLUSTER_MIN_SIZE", "3")),
        cluster_similarity_threshold=float(
            os.environ.get("CLUSTER_SIMILARITY_THRESHOLD", "0.45")
        ),
        draft_model=os.environ.get("DRAFT_MODEL", "claude-opus-4-7"),
        git_auto_push=os.environ.get("GIT_AUTO_PUSH", "true").lower() == "true",
        git_remote=os.environ.get("GIT_REMOTE", "origin"),
        git_branch=os.environ.get("GIT_BRANCH", "main"),
    )
