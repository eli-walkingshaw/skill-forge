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

