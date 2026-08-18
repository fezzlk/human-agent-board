#!/usr/bin/env python3
"""CLI for human-agent-board: a git-backed queue for requests between a human
user and AI coding agents (Claude Code, Codex, etc.)."""

import argparse
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

DIRECTIONS = ("user-to-agent", "agent-to-user")
WORK_STATES = (
    "waiting",
    "researching",
    "implementing",
    "verifying",
    "decision_pending",
    "pr_open",
    "completed",
    "failed",
)
TERMINAL_WORK_STATES = ("completed", "failed")
DECISION_TYPES = ("approval_request", "decision_request", "plan_request")

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

    requires_decision = item.get("type") in DECISION_TYPES

    if related_links and requires_decision:
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
        if related_links:
            messages.append({
                "type": "text",
                "text": _format_related_links(related_links),
            })

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


def status_current_dir() -> Path:
    return board_root() / "status" / "current"


def status_history_dir() -> Path:
    return board_root() / "status" / "history"


def _safe_status_key(value, field_name):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value or ""):
        raise ValueError(f"{field_name} must contain only letters, numbers, ., _, or -")
    return value


def _status_path(source, work_id):
    source = _safe_status_key(source, "source")
    work_id = _safe_status_key(work_id, "work_id")
    return status_current_dir() / f"{source}__{work_id}.yaml"


def set_status(source, work_id, state, title, summary, next_action=None,
               related_links=None, notify=False):
    if state not in WORK_STATES:
        raise ValueError(f"state must be one of: {', '.join(WORK_STATES)}")

    now = datetime.now(timezone.utc)
    item = {
        "source": source,
        "work_id": work_id,
        "state": state,
        "title": title,
        "summary": summary,
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if next_action:
        item["next_action"] = next_action
    if related_links:
        item["related_links"] = list(related_links)

    current_path = _status_path(source, work_id)
    if state in TERMINAL_WORK_STATES:
        directory = status_history_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            f"{now.strftime('%Y%m%dT%H%M%SZ')}_{source}__{work_id}_"
            f"{secrets.token_hex(3)}.yaml"
        )
        if current_path.exists():
            current_path.unlink()
    else:
        current_path.parent.mkdir(parents=True, exist_ok=True)
        path = current_path

    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(item, f, allow_unicode=True, sort_keys=False)

    if notify:
        notify_line(
            {
                "type": "status_update",
                "title": f"{work_id}: {title}",
                "body": f"[{state}] {summary}",
                "related_links": item.get("related_links", []),
            },
            path.name,
        )
    return path.name


def _load_status_files(directory):
    if not directory.exists():
        return []
    items = []
    for path in directory.glob("*.yaml"):
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data["filename"] = path.name
        items.append(data)
    items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return items


def list_statuses(source=None, recent=5):
    current = _load_status_files(status_current_dir())
    history = _load_status_files(status_history_dir())
    if source:
        current = [item for item in current if item.get("source") == source]
        history = [item for item in history if item.get("source") == source]
    return current, history[:recent]


def _format_status_item(item):
    lines = [
        f"[{item.get('state', 'unknown')}] {item.get('work_id', '?')}: "
        f"{item.get('title', '(no title)')}",
        f"  {item.get('summary', '(no summary)')}",
    ]
    if item.get("next_action"):
        lines.append(f"  次: {item['next_action']}")
    lines.append(f"  更新: {item.get('updated_at', 'unknown')}")
    for index, url in enumerate(item.get("related_links") or [], start=1):
        lines.append(f"  {_related_link_label(url, index)}: {url}")
    return "\n".join(lines)


def format_status_list(source=None, recent=5):
    current, history = list_statuses(source=source, recent=recent)
    if not current and not history:
        return "kobitoの作業状況はありません。" if source == "kobito" else "作業状況はありません。"

    sections = []
    if current:
        sections.append("進行中\n" + "\n\n".join(_format_status_item(i) for i in current))
    if history:
        sections.append("直近の完了・失敗\n" + "\n\n".join(_format_status_item(i) for i in history))
    return "\n\n".join(sections)


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


def _safe_item_filename(filename):
    if not filename or Path(filename).name != filename or not filename.endswith(".yaml"):
        raise ValueError("invalid board item filename")
    return filename


def get_item(filename, direction=None):
    filename = _safe_item_filename(filename)
    directions = (direction,) if direction else DIRECTIONS
    for candidate_direction in directions:
        if candidate_direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of: {', '.join(DIRECTIONS)}")
        path = direction_dir(candidate_direction) / filename
        if path.exists():
            with path.open(encoding="utf-8") as f:
                item = yaml.safe_load(f) or {}
            item["filename"] = filename
            item["direction"] = candidate_direction
            return item
    raise FileNotFoundError(f"{filename} not found")


def list_items_full(direction):
    directory = direction_dir(direction)
    if not directory.exists():
        return []

    items = []
    for path in directory.glob("*.yaml"):
        items.append(get_item(path.name, direction=direction))
    items.sort(key=lambda item: item["filename"])
    return items


def list_items(direction):
    return [
        {
            "filename": item["filename"],
            "from": item.get("from"),
            "type": item.get("type"),
            "title": item.get("title"),
            "created_at": item.get("created_at"),
        }
        for item in list_items_full(direction)
    ]


def dashboard_data(recent=5):
    agent_to_user = list_items_full("agent-to-user")
    user_to_agent = list_items_full("user-to-agent")
    current, history = list_statuses(source="kobito", recent=recent)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decisions": [
            item for item in agent_to_user if item.get("type") in DECISION_TYPES
        ],
        "notifications": [
            item for item in agent_to_user if item.get("type") not in DECISION_TYPES
        ],
        "user_requests": user_to_agent,
        "status_current": current,
        "status_history": history,
    }


def respond_to_item(filename, decision):
    if decision not in ("approval", "rejection"):
        raise ValueError("decision must be approval or rejection")
    item = get_item(filename, direction="agent-to-user")
    if item.get("type") not in DECISION_TYPES:
        raise ValueError("board item does not require a decision")

    label = "承認" if decision == "approval" else "却下"
    response_filename = add_item(
        direction="user-to-agent",
        from_="user",
        type_=decision,
        title=f"{label} (LINE経由): {item.get('title', '')}",
        body=f"LINE Boardから{label}されました。",
        related_links=item.get("related_links"),
    )
    complete_item(filename)
    return response_filename


def complete_item(filename):
    filename = _safe_item_filename(filename)
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


def _cmd_get(args):
    item = get_item(args.filename, direction=args.direction)
    if args.json:
        print(json.dumps(item, ensure_ascii=False))
    else:
        print(yaml.safe_dump(item, allow_unicode=True, sort_keys=False).rstrip())


def _cmd_respond(args):
    print(respond_to_item(args.filename, args.decision))


def _cmd_dashboard(args):
    data = dashboard_data(recent=args.recent)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return
    print(
        f"判断待ち {len(data['decisions'])} / "
        f"通知 {len(data['notifications'])} / "
        f"ユーザー依頼 {len(data['user_requests'])} / "
        f"kobito進行中 {len(data['status_current'])}"
    )


def _cmd_status_set(args):
    filename = set_status(
        source=args.source,
        work_id=args.work_id,
        state=args.state,
        title=args.title,
        summary=args.summary,
        next_action=args.next_action,
        related_links=args.related_link,
        notify=args.notify,
    )
    print(filename)


def _cmd_status_list(args):
    print(format_status_list(source=args.source, recent=args.recent))


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

    get_parser = subparsers.add_parser("get", help="Show a board item")
    get_parser.add_argument("filename")
    get_parser.add_argument("--direction", choices=DIRECTIONS)
    get_parser.add_argument("--json", action="store_true")
    get_parser.set_defaults(func=_cmd_get)

    respond_parser = subparsers.add_parser("respond", help="Approve or reject a decision item")
    respond_parser.add_argument("filename")
    respond_parser.add_argument("--decision", choices=("approval", "rejection"), required=True)
    respond_parser.set_defaults(func=_cmd_respond)

    dashboard_parser = subparsers.add_parser("dashboard", help="Show board dashboard data")
    dashboard_parser.add_argument("--recent", type=int, default=5)
    dashboard_parser.add_argument("--json", action="store_true")
    dashboard_parser.set_defaults(func=_cmd_dashboard)

    status_parser = subparsers.add_parser("status", help="Manage agent work status")
    status_subparsers = status_parser.add_subparsers(dest="status_command", required=True)

    status_set_parser = status_subparsers.add_parser("set", help="Create or update work status")
    status_set_parser.add_argument("--source", required=True)
    status_set_parser.add_argument("--work-id", required=True)
    status_set_parser.add_argument("--state", choices=WORK_STATES, required=True)
    status_set_parser.add_argument("--title", required=True)
    status_set_parser.add_argument("--summary", required=True)
    status_set_parser.add_argument("--next-action")
    status_set_parser.add_argument("--related-link", action="append", default=None)
    status_set_parser.add_argument("--notify", action="store_true")
    status_set_parser.set_defaults(func=_cmd_status_set)

    status_list_parser = status_subparsers.add_parser("list", help="List current and recent work status")
    status_list_parser.add_argument("--source")
    status_list_parser.add_argument("--recent", type=int, default=5)
    status_list_parser.set_defaults(func=_cmd_status_list)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
