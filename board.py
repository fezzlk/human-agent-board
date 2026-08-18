#!/usr/bin/env python3
"""CLI for human-agent-board: a git-backed queue for requests between a human
user and AI coding agents (Claude Code, Codex, etc.)."""

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

DIRECTIONS = ("user-to-agent", "agent-to-user")

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_LINE_TITLE_MAX = 40
_LINE_TEXT_MAX = 60
_LINE_DETAILS_MAX = 5000


def _truncate(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _related_link_label(url, index):
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    if hostname == "github.com" or hostname.endswith(".github.com"):
        return "GitHub"
    if hostname == "linear.app" or hostname.endswith(".linear.app"):
        return "Linear"
    return f"関連資料 {index}"


def _format_related_links(related_links):
    lines = ["判断材料（内容を確認してから承認・却下してください）"]
    for index, url in enumerate(related_links, start=1):
        lines.append(f"{_related_link_label(url, index)}: {url}")
    return _truncate("\n".join(lines), _LINE_DETAILS_MAX)


def notify_line(item, filename):
    """Best-effort LINE push for a new agent-to-user item. No-op if the LINE
    env vars aren't configured, and never raises -- a notification failure
    must not affect the CLI's core add/list/complete behavior."""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_NOTIFY_USER_ID", "")
    if not token or not user_id:
        return

    related_links = item.get("related_links") or []
    title = _truncate(item.get("title"), _LINE_TITLE_MAX)
    text = _truncate(item.get("body"), _LINE_TEXT_MAX)

    if related_links:
        messages = [{
            "type": "template",
            "altText": title or "human-agent-board",
            "template": {
                "type": "buttons",
                "title": title or "human-agent-board",
                "text": text or "(no details)",
                "actions": [
                    {
                        "type": "postback",
                        "label": "承認",
                        "data": f"approve|{related_links[0]}",
                    },
                    {
                        "type": "postback",
                        "label": "却下",
                        "data": f"reject|{related_links[0]}",
                    },
                ],
            },
        }, {
            "type": "text",
            "text": _format_related_links(related_links),
        }]
    else:
        messages = [{
            "type": "text",
            "text": f"{title}\n{text}".strip() or "human-agent-board: new item",
        }]

    payload = json.dumps({"to": user_id, "messages": messages}).encode("utf-8")
    request = urllib.request.Request(
        LINE_PUSH_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=10)
    except (urllib.error.URLError, OSError) as e:
        print(f"notify_line: failed to push LINE notification: {e}", file=sys.stderr)


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

    if direction == "agent-to-user":
        notify_line(item, filename)

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
