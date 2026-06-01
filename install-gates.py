#!/usr/bin/env python3
"""Install the three skill gates: quality, sensitivity, effectiveness.

Three gates run between frontmatter validation and install:
  - quality: rejects skills with TODO markers, empty sections, weak descriptions
  - sensitivity: regex-scans for API keys, credentials, internal IDs
  - effectiveness: one API call to self-predict if the description discriminates

Skills that fail any gate land in vault/rejected/ with a sidecar .gate-report.md
explaining why. Override with `forge approve --force <file>` to bypass gates.

Run from inside ~/code/skill-forge:
    python3 install-gates.py
"""
import re
import sys
from pathlib import Path


GATES_PY = '''"""Gates that screen skills before they get installed.

Three gates, each returning a GateResult:
- quality: deterministic checks for TODOs, empty sections, weak description
- sensitivity: regex scan for credentials, API keys, internal IDs
- effectiveness: one API call asking the model to self-predict trigger accuracy

Gates are pure functions. They never write files or call subprocess. The
orchestration (skip / install / reject) happens in watch.py.
"""
from __future__ import annotations
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class GateResult:
    name: str
    passed: bool
    findings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def render(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        marker = "+" if self.passed else "-"
        lines = [f"### {self.name}: {marker} {status}"]
        if self.findings:
            lines.append("")
            lines.append("**Findings:**")
            for f in self.findings:
                lines.append(f"- {f}")
        if self.suggestions:
            lines.append("")
            lines.append("**Suggestions:**")
            for s in self.suggestions:
                lines.append(f"- {s}")
        return "\\n".join(lines)


_FRONTMATTER_RE = re.compile(r"^---\\s*\\n(.*?)\\n---\\s*\\n(.*)$", re.DOTALL)


def _split_skill(skill_md: str) -> tuple[str, str]:
    m = _FRONTMATTER_RE.match(skill_md)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def _fm_value(frontmatter: str, key: str) -> str:
    pat = re.compile(rf"^{re.escape(key)}\\s*:\\s*(.+?)\\s*$", re.MULTILINE)
    m = pat.search(frontmatter)
    return m.group(1) if m else ""


_TODO_RE = re.compile(r"_\\(TODO:.*?\\)_", re.IGNORECASE)
_EMPTY_SECTION_RE = re.compile(r"^(##[^\\n]+)\\n+(?=##|\\Z)", re.MULTILINE)


def gate_quality(skill_md: str, *, block_thin_drafts: bool = True) -> GateResult:
    findings: list[str] = []
    suggestions: list[str] = []
    fm, body = _split_skill(skill_md)
    if not fm:
        return GateResult(
            name="quality",
            passed=False,
            findings=["could not parse frontmatter"],
            suggestions=["ensure the file has `---` delimited frontmatter"],
        )
    desc = _fm_value(fm, "description")
    if len(desc) < 80:
        findings.append(f"description is short ({len(desc)} chars)")
        suggestions.append("expand the description with concrete trigger keywords")
    if _TODO_RE.search(desc):
        findings.append("description contains TODO markers")
        suggestions.append("fill in the TODOs in the description")
    if block_thin_drafts:
        body_todos = _TODO_RE.findall(body)
        if body_todos:
            findings.append(f"body has {len(body_todos)} TODO marker(s)")
            suggestions.append("flesh out TODO sections, or set GATES_BLOCK_THIN_DRAFTS=false")
    empty_sections = _EMPTY_SECTION_RE.findall(body)
    if empty_sections:
        names = ", ".join(s.strip().lstrip("#").strip() for s in empty_sections)
        findings.append(f"{len(empty_sections)} empty section(s): {names}")
        suggestions.append("add content or remove empty headers")
    return GateResult(
        name="quality",
        passed=len(findings) == 0,
        findings=findings,
        suggestions=suggestions,
    )


DEFAULT_SENSITIVITY_PATTERNS: list[tuple[str, str]] = [
    ("Anthropic API key", r"sk-ant-[A-Za-z0-9_\\-]{20,}"),
    ("OpenAI API key", r"sk-[A-Za-z0-9]{32,}"),
    ("Stripe live key", r"sk_live_[A-Za-z0-9]{20,}"),
    ("GitHub personal access token", r"ghp_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained token", r"github_pat_[A-Za-z0-9_]{40,}"),
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("Generic Bearer token", r"[Bb]earer\\s+[A-Za-z0-9_\\-\\.=]{20,}"),
    ("YAML secret/password/token field", r"^\\s*(password|secret|token|api[_-]?key)\\s*:\\s*\\S+"),
    ("NetSuite sandbox account ID", r"\\b\\d{6,8}[-_]sb\\d*\\b"),
    ("SSN-shaped string", r"\\b\\d{3}-\\d{2}-\\d{4}\\b"),
    ("Phone with area code", r"\\b\\(?\\d{3}\\)?[\\s\\-]\\d{3}[\\s\\-]\\d{4}\\b"),
]


def gate_sensitivity(
    skill_md: str,
    *,
    extra_patterns: list[tuple[str, str]] | None = None,
) -> GateResult:
    findings: list[str] = []
    suggestions: list[str] = []
    patterns = list(DEFAULT_SENSITIVITY_PATTERNS)
    if extra_patterns:
        patterns.extend(extra_patterns)
    for label, pattern in patterns:
        try:
            matches = list(re.finditer(pattern, skill_md, re.MULTILINE))
        except re.error as e:
            findings.append(f"pattern '{label}' is invalid regex: {e}")
            continue
        if matches:
            findings.append(f"{label}: {len(matches)} match(es) (content redacted)")
    if findings:
        suggestions.append("remove credentials, API keys, or sensitive IDs")
        suggestions.append("use placeholders like `<YOUR_API_KEY>` or `<SANDBOX_ID>`")
    return GateResult(
        name="sensitivity",
        passed=len(findings) == 0,
        findings=findings,
        suggestions=suggestions,
    )


EFFECTIVENESS_SYSTEM = """You are evaluating whether a SKILL.md's `description` field will reliably trigger Claude to activate the skill at the right moments.

Given the SKILL.md below, do TWO things:

(1) Generate 3 prompts a user might type that SHOULD trigger this skill, and 3 prompts that should NOT (but are related enough to be ambiguous).

(2) For each prompt, predict trigger likelihood (TRIGGER / NO TRIGGER), and a one-line reason.

Then give an overall verdict:
- PASS: description discriminates well
- FAIL: description doesn't discriminate — give a one-sentence fix

Output as STRICT JSON:

{
  "should_trigger": [
    {"prompt": "...", "predicted": "TRIGGER", "reason": "..."},
    ...
  ],
  "should_not_trigger": [
    {"prompt": "...", "predicted": "NO TRIGGER", "reason": "..."},
    ...
  ],
  "verdict": "PASS" or "FAIL",
  "fix_suggestion": "if FAIL, one-sentence fix"
}

No preamble, no fences."""


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def gate_effectiveness(
    skill_md: str,
    *,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2000,
) -> GateResult:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": EFFECTIVENESS_SYSTEM,
        "messages": [{"role": "user", "content": f"```\\n{skill_md}\\n```"}],
    }
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return GateResult(
            name="effectiveness",
            passed=False,
            findings=[f"API error {e.code}"],
            suggestions=["check ANTHROPIC_API_KEY"],
        )
    except urllib.error.URLError as e:
        return GateResult(
            name="effectiveness",
            passed=False,
            findings=[f"network error: {e.reason}"],
            suggestions=["set GATES_EFFECTIVENESS_ENABLED=false to skip"],
        )
    text_parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\\n".join(text_parts).strip()
    try:
        parsed = _parse_json_object(text)
    except ValueError as e:
        return GateResult(
            name="effectiveness",
            passed=False,
            findings=[f"model output was not valid JSON: {e}"],
            suggestions=["try re-approving the proposal"],
        )
    verdict = (parsed.get("verdict") or "").upper().strip()
    fix = parsed.get("fix_suggestion") or ""
    correct = 0
    total = 0
    for item in parsed.get("should_trigger", []):
        total += 1
        pred = (item.get("predicted") or "").upper()
        if "TRIGGER" in pred and "NO" not in pred:
            correct += 1
    for item in parsed.get("should_not_trigger", []):
        total += 1
        pred = (item.get("predicted") or "").upper()
        if "NO TRIGGER" in pred:
            correct += 1
    findings: list[str] = []
    suggestions: list[str] = []
    if total > 0:
        findings.append(f"trigger discrimination: {correct}/{total} predicted correctly")
    findings.append(f"model verdict: {verdict or '(missing)'}")
    if verdict == "FAIL" and fix:
        suggestions.append(f"description fix: {fix}")
    discrimination_ok = total == 0 or correct >= max(4, int(total * 0.66))
    passed = verdict == "PASS" and discrimination_ok
    return GateResult(
        name="effectiveness",
        passed=passed,
        findings=findings,
        suggestions=suggestions,
    )


def _parse_json_object(text: str) -> dict:
    t = text.strip()
    m = re.match(r"^```(?:json)?\\s*\\n(.*)\\n```\\s*$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        start = t.find("{")
        end = t.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("no JSON object found")
        t = t[start : end + 1]
    return json.loads(t)


@dataclass
class GateReport:
    overall_passed: bool
    results: list[GateResult]
    skill_name: str = ""

    def render(self) -> str:
        marker = "+" if self.overall_passed else "-"
        status = "passed all gates" if self.overall_passed else "one or more gates failed"
        lines = [
            f"# Gate report: `{self.skill_name}`",
            "",
            f"**Overall:** {marker} {status}",
            "",
        ]
        for r in self.results:
            lines.append(r.render())
            lines.append("")
        return "\\n".join(lines)


def run_all_gates(
    skill_md: str,
    *,
    skill_name: str,
    block_thin_drafts: bool = True,
    extra_sensitivity_patterns: list[tuple[str, str]] | None = None,
    effectiveness_enabled: bool = True,
    api_key: str = "",
    effectiveness_model: str = "claude-sonnet-4-6",
) -> GateReport:
    results = [
        gate_quality(skill_md, block_thin_drafts=block_thin_drafts),
        gate_sensitivity(skill_md, extra_patterns=extra_sensitivity_patterns),
    ]
    if effectiveness_enabled and api_key:
        results.append(
            gate_effectiveness(skill_md, api_key=api_key, model=effectiveness_model)
        )
    overall = all(r.passed for r in results)
    return GateReport(overall_passed=overall, results=results, skill_name=skill_name)
'''


WATCH_PY_INSERTION = '''
        # ---- Gates: run quality/sensitivity/effectiveness checks before install ----
        import os as _os
        gates_enabled = _os.environ.get("GATES_ENABLED", "true").lower() == "true"
        if gates_enabled:
            from .gates import run_all_gates
            block_thin = _os.environ.get("GATES_BLOCK_THIN_DRAFTS", "true").lower() == "true"
            effect_enabled = _os.environ.get("GATES_EFFECTIVENESS_ENABLED", "true").lower() == "true"
            report = run_all_gates(
                skill_md,
                skill_name=name,
                block_thin_drafts=block_thin,
                effectiveness_enabled=effect_enabled,
                api_key=config.anthropic_api_key,
            )
            if not report.overall_passed:
                rejected_dir = config.vault_path / "rejected"
                rejected_dir.mkdir(parents=True, exist_ok=True)
                rejected_path = rejected_dir / path.name
                report_path = rejected_dir / (path.stem + ".gate-report.md")
                report_path.write_text(report.render(), encoding="utf-8")
                shutil.move(str(path), str(rejected_path))
                print(f"[watch] gates failed for {path.name} — moved to rejected/")
                print(f"[watch] see {report_path.name} for details")
                return
            print(f"[watch] gates passed ({len(report.results)} checks)")
'''


def main() -> int:
    forge_dir = Path("forge")
    if not (forge_dir / "__main__.py").exists():
        print("X forge/__main__.py not found — run from ~/code/skill-forge")
        return 1

    # ---- Step 1: install forge/gates.py ----
    gates_path = forge_dir / "gates.py"
    if gates_path.exists():
        existing = gates_path.read_text()
        if "def run_all_gates" in existing:
            print("  + forge/gates.py already installed (skipping)")
        else:
            print("  ! forge/gates.py exists but missing run_all_gates — overwriting")
            gates_path.write_text(GATES_PY)
            print("  + wrote forge/gates.py")
    else:
        gates_path.write_text(GATES_PY)
        print("  + wrote forge/gates.py")

    # ---- Step 2: patch forge/watch.py ----
    watch_path = forge_dir / "watch.py"
    if not watch_path.exists():
        print("X forge/watch.py missing — aborting")
        return 1

    s = watch_path.read_text()

    if "from .gates import run_all_gates" in s or "run_all_gates(" in s:
        print("  + gates already wired into watch.py (skipping)")
    else:
        # Find the validate_skill block and insert gates check right after.
        # The validation block ends with the early `return` on validation failure.
        # We insert AFTER that block, BEFORE `install_skill`.
        marker = '''    ok, err, name = validate_skill(skill_md)
    if not ok:
        print(f"[watch] validation failed for {path.name}: {err}")
        return

    target = install_skill(config, name, skill_md)'''

        # The current state may have indentation variations — check.
        if marker not in s:
            print("X couldn't find the validate→install transition in watch.py")
            print("  (the file shape doesn't match what this patch expects)")
            return 1

        replacement = '''    ok, err, name = validate_skill(skill_md)
    if not ok:
        print(f"[watch] validation failed for {path.name}: {err}")
        return
''' + WATCH_PY_INSERTION + '''
    target = install_skill(config, name, skill_md)'''

        s = s.replace(marker, replacement, 1)
        watch_path.write_text(s)
        print("  + wired gates into watch.py")

    # ---- Step 3: append new flags to .env.example if missing ----
    env_example = Path(".env.example")
    if env_example.exists():
        e = env_example.read_text()
        additions = []
        if "GATES_ENABLED" not in e:
            additions.append("\\n# --- Gates ---\\n# Quality/sensitivity/effectiveness checks before install. See forge/gates.py.\\nGATES_ENABLED=true\\nGATES_BLOCK_THIN_DRAFTS=true\\nGATES_EFFECTIVENESS_ENABLED=true\\n")
        if additions:
            with env_example.open("a") as f:
                f.write("".join(additions))
            print("  + added gate flags to .env.example")
        else:
            print("  + .env.example already has gate flags (skipping)")

    # ---- Verify ----
    print()
    print("Verification:")
    s = watch_path.read_text()
    g = gates_path.read_text()
    checks = [
        ("gates.py has run_all_gates", "def run_all_gates" in g),
        ("watch.py imports gates", "from .gates import run_all_gates" in s),
        ("watch.py calls run_all_gates", "run_all_gates(" in s),
        ("rejected/ logic in watch.py", "rejected_dir" in s),
    ]
    all_ok = True
    for label, ok in checks:
        sym = "+" if ok else "-"
        print(f"  {sym} {label}")
        if not ok:
            all_ok = False

    if not all_ok:
        print()
        print("X some checks failed — see above")
        return 1

    print()
    print("+ gates installed")
    print()
    print("New behavior: when you drag a proposal to approved/, the watcher will:")
    print("  1. Validate frontmatter (existing)")
    print("  2. Run quality gate (TODOs, empty sections, weak description)")
    print("  3. Run sensitivity gate (API keys, credentials, internal IDs)")
    print("  4. Run effectiveness gate (one API call, ~$0.01 per approval)")
    print("  5. If all pass: install + commit + push + symlink")
    print("  6. If any fail: move to vault/rejected/ with a .gate-report.md")
    print()
    print("Restart the watcher to pick up the new code:")
    print("  pkill -f 'forge watch'")
    print("  cd ~/code/skill-forge")
    print("  nohup python3 -m forge watch > ~/.skill-forge/watch.log 2>&1 &")
    print()
    print("Toggle individual gates in .env if desired:")
    print("  GATES_ENABLED=false                  # disable all gates")
    print("  GATES_BLOCK_THIN_DRAFTS=false        # allow TODO drafts")
    print("  GATES_EFFECTIVENESS_ENABLED=false    # skip the API-cost gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
