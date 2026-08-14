#!/usr/bin/env python3
"""CLI for human-agent-board: a git-backed queue for requests between a human
user and AI coding agents (Claude Code, Codex, etc.)."""

import argparse
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

DIRECTIONS = ("user-to-agent", "agent-to-user")


def board_root() -> Path:
    override = os.environ.get("HUMAN_AGENT_BOARD_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "board"


def direction_dir(direction: str) -> Path:
    return board_root() / direction


def add_item(direction, from_, type_, title, body, related_links=None):
    directory = direction_dir(direction)
    directory.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    filename = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}.yaml"
    path = directory / filename

    item = {
        "from": from_,
        "type": type_,
        "title": title,
        "body": body,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if related_links:
        item["related_links"] = list(related_links)

    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(item, f, allow_unicode=True, sort_keys=False)

    return filename


def list_items(direction):
    directory = direction_dir(direction)
    if not directory.exists():
        return []

    items = []
    for path in directory.glob("*.yaml"):
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        items.append(
            {
                "filename": path.name,
                "from": data.get("from"),
                "type": data.get("type"),
                "title": data.get("title"),
                "created_at": data.get("created_at"),
            }
        )

    items.sort(key=lambda item: item["filename"])
    return items


def complete_item(filename):
    for direction in DIRECTIONS:
        path = direction_dir(direction) / filename
        if path.exists():
            path.unlink()
            return direction
    raise FileNotFoundError(f"{filename} not found in either direction")


def _cmd_add(args):
    filename = add_item(
        direction=args.direction,
        from_=getattr(args, "from"),
        type_=args.type,
        title=args.title,
        body=args.body,
        related_links=args.related_link,
    )
    print(filename)


def _cmd_list(args):
    items = list_items(args.direction)
    if not items:
        print("(no pending items)")
        return
    for item in items:
        print(f"{item['filename']}\t{item['from']}\t{item['type']}\t{item['title']}")


def _cmd_complete(args):
    direction = complete_item(args.filename)
    print(f"completed ({direction}): {args.filename}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a new item to the board")
    add_parser.add_argument("--direction", choices=DIRECTIONS, required=True)
    add_parser.add_argument("--from", dest="from", required=True)
    add_parser.add_argument("--type", required=True)
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--body", required=True)
    add_parser.add_argument("--related-link", action="append", default=None)
    add_parser.set_defaults(func=_cmd_add)

    list_parser = subparsers.add_parser("list", help="List pending items")
    list_parser.add_argument("--direction", choices=DIRECTIONS, required=True)
    list_parser.set_defaults(func=_cmd_list)

    complete_parser = subparsers.add_parser("complete", help="Mark an item as done")
    complete_parser.add_argument("filename")
    complete_parser.set_defaults(func=_cmd_complete)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
