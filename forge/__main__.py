"""skill-forge CLI."""
from __future__ import annotations
import argparse
import sys

from .config import load_config
from .commands import (
    cmd_init,
    cmd_new,
    cmd_draft,
    cmd_edit,
    cmd_inbox_to_skill,
    cmd_audit,
    cmd_list,
    cmd_status,
    cmd_capture,
    cmd_digest,
    cmd_install_hotkey,
    cmd_tag,
    cmd_stack,
    cmd_subscribe,
    cmd_sync,
    cmd_relate,
    cmd_gates,
)
from .watch import watch_loop


def cmd_watch(args, config) -> int:
    watch_loop(config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge", description="Claude skill workshop")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create vault folders (idempotent)").set_defaults(fn=cmd_init)

    new_p = sub.add_parser("new", help="Scaffold a blank SKILL.md in proposals/")
    new_p.add_argument("name", help="Skill name (kebab-case or any string)")
    new_p.add_argument("--force", action="store_true", help="Overwrite existing proposal of same name")
    new_p.set_defaults(fn=cmd_new)

    draft_p = sub.add_parser("draft", help="Claude drafts a SKILL.md from a one-line description")
    draft_p.add_argument("description", nargs="+", help="What the skill should do")
    draft_p.set_defaults(fn=cmd_draft)

    edit_p = sub.add_parser("edit", help="Pull an installed skill into proposals/ for editing")
    edit_p.add_argument("name", help="Installed skill name")
    edit_p.set_defaults(fn=cmd_edit)

    its_p = sub.add_parser("inbox-to-skill", help="Promote an inbox note into a drafted SKILL.md")
    its_p.add_argument("note", help="Path to the note (or filename if it's in inbox/)")
    its_p.set_defaults(fn=cmd_inbox_to_skill)

    sub.add_parser("audit", help="Claude reviews your skills for gaps/staleness/dupes").set_defaults(fn=cmd_audit)
    sub.add_parser("list", help="List installed skills").set_defaults(fn=cmd_list)
    sub.add_parser("status", help="Show pipeline state").set_defaults(fn=cmd_status)
    sub.add_parser("watch", help="Daemon: validate + commit approved/ → repo").set_defaults(fn=cmd_watch)

    cap_p = sub.add_parser("capture", help="Capture: note + auto-grabbed context → drafted SKILL.md")
    cap_p.add_argument("--note", help="The note (if omitted, popup dialog or stdin)")
    cap_p.set_defaults(fn=cmd_capture)

    dig_p = sub.add_parser("digest", help="Weekly digest of solved-pattern candidates")
    dig_p.add_argument("--days", type=int, default=7, help="How many days back to scan (default 7)")
    dig_p.set_defaults(fn=cmd_digest)

    sub.add_parser("install-hotkey", help="Print instructions for binding Cmd+Option+S").set_defaults(fn=cmd_install_hotkey)

    tag_p = sub.add_parser("tag", help="Generate shared tags across all installed skills (Obsidian connects them automatically)")
    tag_p.add_argument("--apply", action="store_true", help="Write the tags to skill files and commit (default: dry-run)")
    tag_p.set_defaults(fn=cmd_tag)


    stack_p = sub.add_parser("stack", help="Group skills into team-scoped stack repos")
    stack_sub = stack_p.add_subparsers(dest="stack_cmd")
    stack_sub.add_parser("list", help="List discovered stacks and their members")
    stack_diff_p = stack_sub.add_parser("diff", help="Show what publish would do")
    stack_diff_p.add_argument("name", help="Stack name")
    stack_pub_p = stack_sub.add_parser("publish", help="Generate stack repo, commit, push")
    stack_pub_p.add_argument("name", nargs="?", help="Stack name (omit if using --all)")
    stack_pub_p.add_argument("--all", action="store_true", help="Publish every stack")
    stack_sub.add_parser("assign", help="Interactive: assign existing skills to stacks")
    stack_sub.add_parser("sync-tags", help="Regenerate inline #stack tags from frontmatter")
    stack_sub.add_parser("sync-visuals", help="Regenerate stack tags AND banners from frontmatter")
    stack_p.set_defaults(fn=cmd_stack)


    sub_p = sub.add_parser("subscribe", help="Register upstream skill repos")
    subscribe_sub = sub_p.add_subparsers(dest="subscribe_cmd")
    subscribe_sub.add_parser("list", help="List all subscriptions and their state")
    add_p = subscribe_sub.add_parser("add", help="Add a subscription")
    add_p.add_argument("name", help="Slug-name for this subscription")
    add_p.add_argument("url", help="Git URL of the upstream repo")
    add_p.add_argument("--branch", help="Branch to track (default: main)")
    add_p.add_argument("--filter", help="Glob for files to pull (default: **/SKILL.md)")
    pull_p = subscribe_sub.add_parser("pull", help="Pull latest from upstream(s)")
    pull_p.add_argument("name", nargs="?", help="Subscription name")
    pull_p.add_argument("--all", action="store_true", help="Pull all subscriptions")
    rm_p = subscribe_sub.add_parser("remove", help="Unsubscribe + delete clone")
    rm_p.add_argument("name", help="Subscription name")
    pin_p = subscribe_sub.add_parser("pin", help="Freeze at a specific SHA")
    pin_p.add_argument("name", help="Subscription name")
    pin_p.add_argument("sha", help="Full or short SHA")
    unpin_p = subscribe_sub.add_parser("unpin", help="Resume branch tracking")
    unpin_p.add_argument("name", help="Subscription name")
    sub_p.set_defaults(fn=cmd_subscribe)


    sync_p = sub.add_parser("sync", help="Scan subscribed repos, propose new skills into pending/")
    sync_p.add_argument("name", nargs="?", help="Specific subscription to sync (omit for all)")
    sync_p.add_argument("--dry-run", action="store_true", help="Show what would happen, no writes, no API calls")
    sync_p.set_defaults(fn=cmd_sync)


    relate_p = sub.add_parser("relate", help="Use Claude to write related: links into SKILL.md frontmatter")
    relate_p.add_argument("name", nargs="?", help="Just one skill (omit for all)")
    relate_p.add_argument("--dry-run", action="store_true", help="Show plan, no API calls, no writes")
    relate_p.add_argument("--sleep", type=float, default=8.0, help="Seconds between API calls (default 8)")
    relate_p.set_defaults(fn=cmd_relate)


    gates_p = sub.add_parser("gates", help="Run gates on a SKILL.md file (returns 0=pass, 1=fail)")
    gates_p.add_argument("path", help="Path to SKILL.md (bare or wrapped proposal)")
    gates_p.add_argument("--no-effectiveness", action="store_true",
                         help="Skip the effectiveness gate (no Claude API call)")
    gates_p.add_argument("--allow-thin-drafts", action="store_true",
                         help="Don't block thin drafts in the quality gate")
    gates_p.add_argument("--json", action="store_true",
                         help="Output machine-readable JSON instead of human report")
    gates_p.set_defaults(fn=cmd_gates)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except RuntimeError as e:
        print(f"config error: {e}", file=sys.stderr)
        print("Tip: copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2
    return args.fn(args, config)


if __name__ == "__main__":
    sys.exit(main())

