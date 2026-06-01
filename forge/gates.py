"""Gates that screen skills before they get installed.

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
        return "\n".join(lines)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _split_skill(skill_md: str) -> tuple[str, str]:
    m = _FRONTMATTER_RE.match(skill_md)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def _fm_value(frontmatter: str, key: str) -> str:
    pat = re.compile(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$", re.MULTILINE)
    m = pat.search(frontmatter)
    return m.group(1) if m else ""


_TODO_RE = re.compile(r"_\(TODO:.*?\)_", re.IGNORECASE)
_SECTION_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _section_is_truly_empty(body: str, heading_match) -> bool:
    """A section is truly empty if the next non-blank line after the heading
    is another `##` heading (same level) or end-of-body.

    Subheadings (`###`+), tables, code fences, lists, quotes, paragraphs,
    raw HTML — all count as content.
    """
    # Body slice that comes after this heading's line
    after_heading = body[heading_match.end():]
    for raw_line in after_heading.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue  # blank line — keep looking
        # Found a non-blank line. Is it another `##` heading?
        if re.match(r"^##[ \t]+", line):
            return True
        # Anything else (###, text, |, ```, -, >, etc) is content
        return False
    # Reached end-of-body with only blank lines after the heading
    return True


def _find_empty_section_headings(body: str) -> list[str]:
    """Return the heading text of any truly-empty ## sections."""
    out = []
    for m in _SECTION_HEADING_RE.finditer(body):
        if _section_is_truly_empty(body, m):
            out.append(m.group(1).strip())
    return out


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
    empty_sections = _find_empty_section_headings(body)
    if empty_sections:
        names = ", ".join(empty_sections)
        findings.append(f"{len(empty_sections)} empty section(s): {names}")
        suggestions.append("add content or remove empty headers")
    return GateResult(
        name="quality",
        passed=len(findings) == 0,
        findings=findings,
        suggestions=suggestions,
    )


DEFAULT_SENSITIVITY_PATTERNS: list[tuple[str, str]] = [
    ("Anthropic API key", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("OpenAI API key", r"sk-[A-Za-z0-9]{32,}"),
    ("Stripe live key", r"sk_live_[A-Za-z0-9]{20,}"),
    ("GitHub personal access token", r"ghp_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained token", r"github_pat_[A-Za-z0-9_]{40,}"),
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("Generic Bearer token", r"[Bb]earer\s+[A-Za-z0-9_\-\.=]{20,}"),
    ("YAML secret/password/token field", r"^\s*(password|secret|token|api[_-]?key)\s*:\s*\S+"),
    ("NetSuite sandbox account ID", r"\b\d{6,8}[-_]sb\d*\b"),
    ("SSN-shaped string", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("Phone with area code", r"\b\(?\d{3}\)?[\s\-]\d{3}[\s\-]\d{4}\b"),
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
        "messages": [{"role": "user", "content": f"```\n{skill_md}\n```"}],
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
    text = "\n".join(text_parts).strip()
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
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", t, re.DOTALL)
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
        return "\n".join(lines)


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
