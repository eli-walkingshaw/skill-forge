"""End-to-end smoke test: stub the API, run the full pipeline, verify outputs.

Run with: python -m scripts.smoke_e2e
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.config import Config  # noqa: E402
from forge.capture import Capture, write_captures, now_iso  # noqa: E402
from forge.cluster import cluster_captures, write_clusters  # noqa: E402
from forge.drafter import proposal_path, extract_skill_name, wrap_proposal  # noqa: E402
from forge.watch import process_approved_file  # noqa: E402


FAKE_SKILL_MD = """---
name: rhino-svg-uri-encoding
description: Fixes Suitelet white-screen-on-save by percent-encoding special characters in inline SVG data URIs under Rhino/ES5. Use whenever the user mentions a NetSuite Suitelet rendering blank, a SVG inline background, or Rhino crashing on a # character.
---

# Rhino SVG URI Encoding

NetSuite's Rhino/ES5 engine chokes on literal `#` in SVG data URIs.

## When to use
- Suitelet renders a white/blank screen after deploy
- Inline SVG `background-image: url("data:image/svg+xml,...")` in a SuiteScript file
- Rhino throws a parse error near a `#` character

## The pattern
Replace `#` with `%23` and `"` with `%22` inside the data URI.

```js
// Before
'background: url("data:image/svg+xml,<svg fill=\"#006BFF\"...")'

// After
'background: url("data:image/svg+xml,<svg fill=%22%23006BFF%22...")'
```

## Steps
1. Find any inline SVG data URI in the SuiteScript file.
2. Percent-encode `#` → `%23` and `"` → `%22`.
3. Redeploy and verify.

## Gotchas
- Rhino does not support template literals; keep using single quotes.
- The encoding only applies to the URI; not to standalone SVG markup elsewhere.
"""


def fake_api_call(*args, **kwargs):
    """Mock urlopen to return a fake API response."""
    import io
    import json
    body = json.dumps({
        "content": [{"type": "text", "text": FAKE_SKILL_MD}]
    }).encode()

    class FakeResp:
        def read(self):
            return body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
    return FakeResp()


def make_config(td: Path) -> Config:
    vault = td / "vault"
    repo = td / "skills-repo"
    return Config(
        anthropic_api_key="sk-test-fake",
        vault_path=vault,
        skills_repo_path=repo,
        sources=["inbox"],
        claude_code_logs_path=td / "nonexistent",
        scan_days_back=7,
        cluster_min_size=3,
        cluster_similarity_threshold=0.3,
        draft_model="claude-opus-4-7",
        git_auto_push=False,
        git_remote="origin",
        git_branch="main",
    )


def setup_vault(config: Config):
    for d in [config.inbox_dir, config.proposals_dir, config.approved_dir,
              config.archive_dir, config.state_dir, config.skills_repo_path]:
        d.mkdir(parents=True, exist_ok=True)


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def run():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        config = make_config(td_path)
        setup_vault(config)

        # We bypass the state_dir property (which points at $HOME). Override it.
        # Easiest: monkeypatch via __dict__ — but Config is frozen. Use object.__setattr__.
        # Actually properties can't be overridden on instances. We'll just point
        # state_dir to the temp dir by re-rooting it through a subclass trick:
        # the property returns Path.home() / ".skill-forge". We patch Path.home.
        with patch("forge.config.Path.home", return_value=td_path):
            # Step 1: seed inbox with 3 related notes
            for i in range(3):
                (config.inbox_dir / f"note_{i}.md").write_text(
                    "---\n"
                    "goal: Fix Suitelet white screen\n"
                    "tools: SuiteScript, Rhino\n"
                    "---\n"
                    f"Attempt {i}: the SVG data URI had a literal # which Rhino rejected. "
                    f"Replaced # with %23 and the page rendered.\n",
                    encoding="utf-8",
                )

            # Step 2: scan
            from forge.sources import read_all
            captures = read_all(config)
            written = write_captures(captures, config.captures_path)
            expect(written == 3, f"3 captures written (got {written})")

            # Step 3: cluster
            from forge.capture import read_captures
            clusters = cluster_captures(
                read_captures(config.captures_path),
                min_size=config.cluster_min_size,
                threshold=config.cluster_similarity_threshold,
            )
            write_clusters(clusters, config.clusters_path)
            expect(len(clusters) == 1, f"one cluster (got {len(clusters)})")

            # Step 4: draft (with API stubbed)
            from forge import drafter
            with patch.object(drafter.urllib.request, "urlopen", fake_api_call):
                skill_md = drafter.draft_skill(config, clusters[0],
                                               [c for c in read_captures(config.captures_path)])

            name = extract_skill_name(skill_md)
            expect(name == "rhino-svg-uri-encoding", f"name extracted: {name}")

            out = proposal_path(config, clusters[0], name)
            out.write_text(wrap_proposal(skill_md, clusters[0], 3), encoding="utf-8")
            expect(out.exists(), f"proposal written: {out.name}")

            # Step 5: simulate approval (move to approved/)
            approved_path = config.approved_dir / out.name
            out.rename(approved_path)

            # Step 6: process the approved file (skip git since no repo)
            # Init a real git repo in the skills repo path so commit step works.
            import subprocess
            subprocess.run(["git", "init", "-q", str(config.skills_repo_path)], check=True)
            subprocess.run(["git", "-C", str(config.skills_repo_path), "config",
                            "user.email", "test@test"], check=True)
            subprocess.run(["git", "-C", str(config.skills_repo_path), "config",
                            "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(config.skills_repo_path), "checkout",
                            "-q", "-b", "main"], check=True)

            process_approved_file(config, approved_path)

            installed = config.skills_repo_path / "rhino-svg-uri-encoding" / "SKILL.md"
            expect(installed.exists(), f"SKILL.md installed at {installed.relative_to(td_path)}")

            content = installed.read_text()
            expect("name: rhino-svg-uri-encoding" in content, "frontmatter intact in installed file")
            expect("> [!info]" not in content, "proposal wrapper stripped from installed file")

            archived = list(config.archive_dir.glob("installed__*"))
            expect(len(archived) == 1, "approved proposal moved to archive")

    print("\n✓ end-to-end smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
