"""Self-tests for the deterministic parts of skill-forge.

Run with: python -m scripts.test_forge
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

# Make sibling 'forge' importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.capture import Capture, write_captures, read_captures, now_iso  # noqa: E402
from forge.cluster import cluster_captures, tokenize  # noqa: E402
from forge.sources import _parse_inbox_note, _detect_tools, _summarize_assistant  # noqa: E402
from forge.watch import extract_skill_md, validate_skill  # noqa: E402
from forge.drafter import wrap_proposal, extract_skill_name  # noqa: E402
from forge.cluster import Cluster  # noqa: E402


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def test_tokenize():
    print("test_tokenize")
    toks = tokenize("Fixing the SVG data URI percent encoding in SuiteScript")
    expect("svg" in toks, "tokenizes svg")
    expect("the" not in toks, "drops stopword 'the'")
    expect("suitescript" in toks, "lowercases SuiteScript")


def test_capture_dedupe():
    print("test_capture_dedupe")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cap.jsonl"
        c1 = Capture.make(source="inbox", source_ref="a", timestamp=now_iso(),
                          goal="fix the white screen bug", pattern="encode the %23 in SVG URI")
        c2 = Capture.make(source="inbox", source_ref="a", timestamp=now_iso(),
                          goal="fix the white screen bug", pattern="encode the %23 in SVG URI")
        n = write_captures([c1, c2], path)
        expect(n == 1, "second identical capture is deduped")
        loaded = read_captures(path)
        expect(len(loaded) == 1, "only one capture stored")


def test_clustering_finds_repeats():
    print("test_clustering_finds_repeats")
    captures = [
        Capture.make(source="inbox", source_ref=f"r1_{i}", timestamp=now_iso(),
                     goal=f"Suitelet white screen on save attempt {i}",
                     pattern="The SVG data URI had a literal # which Rhino choked on. Replace with %23.",
                     tools=["SuiteScript", "Rhino/ES5"])
        for i in range(3)
    ] + [
        Capture.make(source="inbox", source_ref="other", timestamp=now_iso(),
                     goal="Set up Slack webhook",
                     pattern="Create incoming webhook in Slack admin then POST JSON to it.",
                     tools=["Slack"])
    ]
    clusters = cluster_captures(captures, min_size=3, threshold=0.3)
    expect(len(clusters) == 1, "exactly one cluster meeting min_size")
    expect(clusters[0].size() == 3, "cluster has 3 members")
    expect("svg" in clusters[0].top_terms or "rhino" in [t.lower() for t in clusters[0].top_terms],
           "top terms include domain words")


def test_clustering_below_threshold():
    print("test_clustering_below_threshold")
    captures = [
        Capture.make(source="inbox", source_ref="a", timestamp=now_iso(),
                     goal="completely different topic A", pattern="alpha beta gamma"),
        Capture.make(source="inbox", source_ref="b", timestamp=now_iso(),
                     goal="completely different topic B", pattern="delta epsilon zeta"),
        Capture.make(source="inbox", source_ref="c", timestamp=now_iso(),
                     goal="completely different topic C", pattern="eta theta iota"),
    ]
    clusters = cluster_captures(captures, min_size=3, threshold=0.5)
    expect(len(clusters) == 0, "unrelated captures don't cluster")


def test_inbox_frontmatter():
    print("test_inbox_frontmatter")
    text = """---
goal: Fix the build
tools: SuiteScript, Rhino
---
Body of the note describing the fix.
"""
    goal, body, tools = _parse_inbox_note(text, fallback_goal="x")
    expect(goal == "Fix the build", "frontmatter goal parsed")
    expect("Body" in body, "body extracted")
    expect("SuiteScript" in tools, "tools list parsed")


def test_inbox_no_frontmatter():
    print("test_inbox_no_frontmatter")
    text = "# Header is the goal\n\nAnd the body explains the fix."
    goal, body, tools = _parse_inbox_note(text, fallback_goal="fallback")
    expect(goal == "Header is the goal", "uses first heading as goal")
    expect("body explains" in body, "body preserved")
    expect(tools == [], "no tools when frontmatter absent")


def test_tool_detection():
    print("test_tool_detection")
    tools = _detect_tools("In our Suitelet using SuiteScript I hit a Rhino backtick issue")
    expect("SuiteScript" in tools, "detects SuiteScript")
    expect("Rhino/ES5" in tools, "detects Rhino")


def test_summarize_assistant_grabs_code():
    print("test_summarize_assistant_grabs_code")
    text = "Here is the fix:\n```js\nconst x = 1;\n```\nMore prose after."
    summary = _summarize_assistant(text)
    expect("```" in summary, "preserves code fence")
    expect("const x" in summary, "preserves code content")


def test_validate_skill():
    print("test_validate_skill")
    good = "---\nname: my-skill\ndescription: When to use it.\n---\n\n# My Skill\n"
    ok, err, name = validate_skill(good)
    expect(ok, f"valid skill passes (got: {err})")
    expect(name == "my-skill", "name extracted")

    bad_name = "---\nname: My_Skill\ndescription: x\n---\n\n# X\n"
    ok, err, _ = validate_skill(bad_name)
    expect(not ok, "rejects non-kebab-case name")

    no_fm = "# Just a heading\n"
    ok, _, _ = validate_skill(no_fm)
    expect(not ok, "rejects missing frontmatter")


def test_extract_skill_md_from_proposal():
    print("test_extract_skill_md_from_proposal")
    cluster = Cluster(id="cl_test", member_ids=["a", "b", "c"], fingerprint="abc", top_terms=["x"])
    skill_md = "---\nname: test-skill\ndescription: A test skill.\n---\n\n# Test\n"
    wrapped = wrap_proposal(skill_md, cluster, member_count=3)
    extracted = extract_skill_md(wrapped)
    expect(extracted is not None, "extraction returns something")
    expect(extracted.startswith("---"), "extracted starts with frontmatter")
    expect("name: test-skill" in extracted, "name is preserved")


def test_extract_skill_name_from_md():
    print("test_extract_skill_name_from_md")
    md = "---\nname: rhino-svg-uri-encoding\ndescription: ...\n---"
    expect(extract_skill_name(md) == "rhino-svg-uri-encoding", "extracts name correctly")


def run_all():
    tests = [
        test_tokenize,
        test_capture_dedupe,
        test_clustering_finds_repeats,
        test_clustering_below_threshold,
        test_inbox_frontmatter,
        test_inbox_no_frontmatter,
        test_tool_detection,
        test_summarize_assistant_grabs_code,
        test_validate_skill,
        test_extract_skill_md_from_proposal,
        test_extract_skill_name_from_md,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  ✗ FAIL: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())
