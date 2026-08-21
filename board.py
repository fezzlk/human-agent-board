#!/usr/bin/env python3
"""CLI for human-agent-board: a git-backed queue for requests between a human
user and AI coding agents (Claude Code, Codex, etc.)."""

import argparse
import json
import os
import re
import secrets
import sys
import time
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
RUN_OUTCOMES = ("completed", "failed", "blocked", "no_work")
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

    _push_line_messages(messages, token=token, user_id=user_id)


def _push_line_messages(messages, token=None, user_id=None):
    """Best-effort LINE push of a pre-built messages array. No-op if the LINE
    env vars aren't configured (unless explicitly passed in), and never
    raises -- callers must keep working even if the notification fails."""
    token = token or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = user_id or os.environ.get("LINE_NOTIFY_USER_ID", "")
    if not token or not user_id:
        return

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
        print(f"_push_line_messages: failed to push LINE notification: {e}", file=sys.stderr)


_PRIORITY_LABELS = {1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
_PRIORITY_DIGEST_MAX_BUBBLES = 12
_PRIORITY_BUBBLE_TITLE_MAX = 60


def priority_digest(issues):
    """Push a LINE Flex carousel with one bubble per issue (identifier,
    title, project) and four priority buttons (Urgent/High/Medium/Low) that
    postback `setpriority|<issueId>|<priorityValue>`. No-op on an empty
    list or when LINE env vars aren't configured."""
    issues = list(issues)[:_PRIORITY_DIGEST_MAX_BUBBLES]
    if not issues:
        return

    bubbles = []
    for issue in issues:
        issue_id = issue["id"]
        identifier = issue.get("identifier", "")
        title = _truncate(issue.get("title"), _PRIORITY_BUBBLE_TITLE_MAX)
        project = issue.get("project", "")
        bubbles.append({
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": identifier, "size": "xs", "color": "#9CA3AF"},
                    {"type": "text", "text": title, "weight": "bold", "wrap": True},
                    {"type": "text", "text": project, "size": "xs", "color": "#6B7280"},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary" if value <= 2 else "secondary",
                        "height": "sm",
                        "action": {
                            "type": "postback",
                            "label": label,
                            "data": f"setpriority|{issue_id}|{value}",
                            "displayText": f"{identifier}: {label}",
                        },
                    }
                    for value, label in _PRIORITY_LABELS.items()
                ],
            },
        })

    messages = [{
        "type": "flex",
        "altText": f"優先度未設定のissueが{len(bubbles)}件あります",
        "contents": {"type": "carousel", "contents": bubbles},
    }]
    _push_line_messages(messages)


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


def run_current_dir() -> Path:
    return board_root() / "runs" / "current"


def run_history_dir() -> Path:
    return board_root() / "runs" / "history"


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_path(source, run_id, current=True):
    source = _safe_status_key(source, "source")
    run_id = _safe_status_key(run_id, "run_id")
    directory = run_current_dir() if current else run_history_dir()
    return directory / f"{source}__{run_id}.yaml"


def start_run(source, trigger="unknown", run_id=None, started_at=None):
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    now = started_at or _utc_now()
    item = {
        "source": _safe_status_key(source, "source"),
        "run_id": _safe_status_key(run_id, "run_id"),
        "state": "running",
        "trigger": trigger,
        "started_at": now,
        "heartbeat_at": now,
    }
    path = _run_path(source, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(item, f, allow_unicode=True, sort_keys=False)
    return item


def heartbeat_run(source, run_id, phase=None, summary=None, recorded_at=None):
    path = _run_path(source, run_id)
    if not path.exists():
        raise FileNotFoundError(f"active run not found: {source}/{run_id}")
    with path.open(encoding="utf-8") as f:
        item = yaml.safe_load(f) or {}
    item["heartbeat_at"] = recorded_at or _utc_now()
    if phase:
        item["phase"] = phase
    if summary:
        item["summary"] = summary
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(item, f, allow_unicode=True, sort_keys=False)
    return item


def finish_run(source, run_id, outcome, summary=None, recovery=None, finished_at=None):
    if outcome not in RUN_OUTCOMES:
        raise ValueError(f"outcome must be one of: {', '.join(RUN_OUTCOMES)}")
    current_path = _run_path(source, run_id)
    if not current_path.exists():
        raise FileNotFoundError(f"active run not found: {source}/{run_id}")
    with current_path.open(encoding="utf-8") as f:
        item = yaml.safe_load(f) or {}
    now = finished_at or _utc_now()
    item.update({"state": outcome, "heartbeat_at": now, "finished_at": now})
    if summary:
        item["summary"] = summary
    if recovery:
        item["recovery"] = recovery
    history_path = _run_path(source, run_id, current=False)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(item, f, allow_unicode=True, sort_keys=False)
    current_path.unlink()
    return item


def _load_runs(directory):
    if not directory.exists():
        return []
    items = []
    for path in directory.glob("*.yaml"):
        with path.open(encoding="utf-8") as f:
            item = yaml.safe_load(f) or {}
        item["filename"] = path.name
        items.append(item)
    return sorted(items, key=lambda item: item.get("started_at", ""), reverse=True)


def run_health(source, stale_after=900, missing_after=14400, recent=10, now=None):
    now_ts = now if now is not None else time.time()
    active = [item for item in _load_runs(run_current_dir()) if item.get("source") == source]
    history = [item for item in _load_runs(run_history_dir()) if item.get("source") == source]
    history = history[:recent]
    stale = []
    for item in active:
        try:
            heartbeat_ts = datetime.fromisoformat(item["heartbeat_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            heartbeat_ts = 0
        item["heartbeat_age_seconds"] = max(0, int(now_ts - heartbeat_ts))
        item["stale"] = item["heartbeat_age_seconds"] > stale_after
        if item["stale"]:
            stale.append(item)
    latest = history[0] if history else None
    consecutive_failures = 0
    for item in history:
        if item.get("state") not in ("failed", "blocked"):
            break
        consecutive_failures += 1
    last_seen_at = None
    candidates = [i.get("heartbeat_at") for i in active] + [i.get("finished_at") for i in history]
    candidates = [value for value in candidates if value]
    if candidates:
        last_seen_at = max(candidates)
    missing = False
    if last_seen_at:
        last_seen_ts = datetime.fromisoformat(last_seen_at.replace("Z", "+00:00")).timestamp()
        missing = now_ts - last_seen_ts > missing_after
    if stale:
        health = "stale"
    elif active:
        health = "running"
    elif missing or not last_seen_at:
        health = "missing"
    elif consecutive_failures:
        health = "degraded"
    else:
        health = "healthy"
    return {
        "source": source, "health": health, "last_seen_at": last_seen_at,
        "consecutive_failures": consecutive_failures, "active": active,
        "recent": history, "latest": latest,
    }


def usage_snapshots_path() -> Path:
    return board_root() / "usage" / "snapshots.jsonl"


def vm_events_path() -> Path:
    return board_root() / "vm" / "events.jsonl"


def record_vm_event(action, result, instance, project, zone, recorded_at=None):
    if action not in ("create", "delete"):
        raise ValueError("action must be create or delete")
    if result not in ("success", "failed"):
        raise ValueError("result must be success or failed")
    item = {
        "action": action,
        "result": result,
        "instance": instance,
        "project": project,
        "zone": zone,
        "recorded_at": recorded_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = vm_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def list_vm_events(recent=10):
    if recent < 1:
        raise ValueError("recent must be at least 1")
    path = vm_events_path()
    if not path.exists():
        return []
    items = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if all(key in item for key in ("action", "result", "instance", "recorded_at")):
                items.append(item)
    # JSONL append order is authoritative. Timestamps only have second
    # precision, so sorting them can misorder a rapid create/delete pair.
    return list(reversed(items))[:recent]


def record_usage(provider, primary_used, secondary_used=None,
                 primary_resets_at=None, secondary_resets_at=None,
                 work_id=None, source="collector", recorded_at=None):
    if provider not in ("claude", "codex"):
        raise ValueError("provider must be claude or codex")
    for name, value in (("primary_used", primary_used), ("secondary_used", secondary_used)):
        if value is not None and not 0 <= float(value) <= 100:
            raise ValueError(f"{name} must be between 0 and 100")
    item = {
        "provider": provider,
        "recorded_at": recorded_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "primary_used": float(primary_used),
        "source": source,
    }
    if secondary_used is not None:
        item["secondary_used"] = float(secondary_used)
    if primary_resets_at is not None:
        item["primary_resets_at"] = primary_resets_at
    if secondary_resets_at is not None:
        item["secondary_resets_at"] = secondary_resets_at
    if work_id:
        item["work_id"] = _safe_status_key(work_id, "work_id")
    path = usage_snapshots_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def list_usage(provider=None, hours=720):
    path = usage_snapshots_path()
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - max(1, hours) * 3600
    items = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                timestamp = datetime.fromisoformat(item["recorded_at"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            if timestamp >= cutoff and (not provider or item.get("provider") == provider):
                items.append(item)
    return sorted(items, key=lambda item: item["recorded_at"])


def usage_dashboard(hours=720):
    snapshots = list_usage(hours=hours)
    latest = {}
    for item in snapshots:
        latest[item["provider"]] = item
    grouped = {}
    for item in snapshots:
        if item.get("work_id"):
            grouped.setdefault((item["provider"], item["work_id"]), []).append(item)
    tasks = []
    for (provider, work_id), values in grouped.items():
        first, last = values[0], values[-1]
        same_window = first.get("primary_resets_at") == last.get("primary_resets_at")
        delta = last["primary_used"] - first["primary_used"] if same_window else None
        tasks.append({
            "provider": provider,
            "work_id": work_id,
            "started_at": first["recorded_at"],
            "ended_at": last["recorded_at"],
            "primary_used_delta": max(0, delta) if delta is not None else None,
            "sample_count": len(values),
        })
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": hours,
        "latest": latest,
        "snapshots": snapshots,
        "tasks": sorted(tasks, key=lambda item: item["ended_at"], reverse=True),
        "savings": {
            "status": "insufficient_data",
            "message": "Board導入前後の比較に必要なデータを蓄積中です。",
        },
    }


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
    previous = []
    if current_path.exists():
        previous.extend(_load_status_files(current_path.parent))
    previous.extend(_load_status_files(status_history_dir()))
    previous_state = next(
        (
            entry.get("state")
            for entry in previous
            if entry.get("source") == source and entry.get("work_id") == work_id
        ),
        None,
    )
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

    # Preserve repeated unattended results in history, but alert only when the
    # state changes. Reworded copies of the same failure should not spam LINE.
    if notify and previous_state != state:
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


def add_item(direction, from_, type_, title, body, related_links=None, dedupe_key=None):
    directory = direction_dir(direction)
    directory.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    if dedupe_key:
        dedupe_key = _safe_status_key(dedupe_key, "dedupe_key")
        existing = next(
            (item for item in list_items_full(direction) if item.get("dedupe_key") == dedupe_key),
            None,
        )
        if existing:
            path = directory / existing["filename"]
            existing.update({
                "from": from_, "type": type_, "title": title, "body": body,
                "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            existing.pop("filename", None)
            existing.pop("direction", None)
            if related_links:
                existing["related_links"] = list(related_links)
            with path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
            return path.name
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
    if dedupe_key:
        item["dedupe_key"] = dedupe_key

    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(item, f, allow_unicode=True, sort_keys=False)

    if direction == "agent-to-user":
        notify_line(item, filename)

    return filename


def resolve_item(direction, dedupe_key):
    dedupe_key = _safe_status_key(dedupe_key, "dedupe_key")
    matches = [item for item in list_items_full(direction) if item.get("dedupe_key") == dedupe_key]
    for item in matches:
        (direction_dir(direction) / item["filename"]).unlink()
    return len(matches)


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
    now_ts = time.time()
    for item in current:
        try:
            updated_ts = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
            item["age_seconds"] = max(0, int(now_ts - updated_ts))
            item["stale"] = item["age_seconds"] > 14400
        except (KeyError, ValueError):
            item["stale"] = True
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
        "kobito_run": run_health("kobito", recent=recent),
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
        dedupe_key=args.dedupe_key,
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


def _cmd_resolve(args):
    count = resolve_item(args.direction, args.dedupe_key)
    print(f"resolved: {count}")


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
        f"kobito {data['kobito_run']['health']} / "
        f"kobito進行中 {len(data['status_current'])}"
    )


def _cmd_run_start(args):
    print(json.dumps(start_run(args.source, trigger=args.trigger), ensure_ascii=False))


def _cmd_run_heartbeat(args):
    print(json.dumps(heartbeat_run(args.source, args.run_id, args.phase, args.summary), ensure_ascii=False))


def _cmd_run_finish(args):
    print(json.dumps(finish_run(args.source, args.run_id, args.outcome, args.summary, args.recovery), ensure_ascii=False))


def _cmd_run_list(args):
    data = run_health(args.source, args.stale_after, args.missing_after, args.recent)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return
    print(f"{args.source}: {data['health']} / 最終確認: {data['last_seen_at'] or 'なし'} / 連続失敗: {data['consecutive_failures']}")
    for item in data["active"]:
        suffix = " (停止疑い)" if item.get("stale") else ""
        print(f"  running {item['run_id']} heartbeat={item.get('heartbeat_at', '?')}{suffix}")
    for item in data["recent"]:
        print(f"  {item.get('state', '?')} {item.get('run_id', '?')} finished={item.get('finished_at', '?')} {item.get('summary', '')}")


def _cmd_usage_record(args):
    item = record_usage(
        provider=args.provider,
        primary_used=args.primary_used,
        secondary_used=args.secondary_used,
        primary_resets_at=args.primary_resets_at,
        secondary_resets_at=args.secondary_resets_at,
        work_id=args.work_id,
        source=args.source,
    )
    print(json.dumps(item, ensure_ascii=False))


def _cmd_usage_dashboard(args):
    print(json.dumps(usage_dashboard(hours=args.hours), ensure_ascii=False))


def _cmd_vm_record(args):
    item = record_vm_event(
        action=args.action,
        result=args.result,
        instance=args.instance,
        project=args.project,
        zone=args.zone,
    )
    print(json.dumps(item, ensure_ascii=False))


def _cmd_vm_list(args):
    items = list_vm_events(recent=args.recent)
    if args.json:
        print(json.dumps(items, ensure_ascii=False))
        return
    labels = {"create": "起動（作成）", "delete": "停止（削除）"}
    for item in items:
        print(
            f"{item['recorded_at']}\t{labels[item['action']]}\t"
            f"{item['result']}\t{item['instance']}\t"
            f"{item.get('project', '?')}/{item.get('zone', '?')}"
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


def _cmd_priority_digest(args):
    issues = json.load(sys.stdin)
    priority_digest(issues)
    print(f"sent digest for {min(len(issues), _PRIORITY_DIGEST_MAX_BUBBLES)} issue(s)")


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
    add_parser.add_argument("--dedupe-key")
    add_parser.set_defaults(func=_cmd_add)

    list_parser = subparsers.add_parser("list", help="List pending items")
    list_parser.add_argument("--direction", choices=DIRECTIONS, required=True)
    list_parser.set_defaults(func=_cmd_list)

    complete_parser = subparsers.add_parser("complete", help="Mark an item as done")
    complete_parser.add_argument("filename")
    complete_parser.set_defaults(func=_cmd_complete)

    resolve_parser = subparsers.add_parser("resolve", help="Resolve items by stable dedupe key")
    resolve_parser.add_argument("--direction", choices=DIRECTIONS, required=True)
    resolve_parser.add_argument("--dedupe-key", required=True)
    resolve_parser.set_defaults(func=_cmd_resolve)

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

    run_parser = subparsers.add_parser("run", help="Track autonomous agent executions")
    run_subparsers = run_parser.add_subparsers(dest="run_command", required=True)
    run_start_parser = run_subparsers.add_parser("start")
    run_start_parser.add_argument("--source", required=True)
    run_start_parser.add_argument("--trigger", default="unknown")
    run_start_parser.set_defaults(func=_cmd_run_start)
    run_heartbeat_parser = run_subparsers.add_parser("heartbeat")
    run_heartbeat_parser.add_argument("--source", required=True)
    run_heartbeat_parser.add_argument("--run-id", required=True)
    run_heartbeat_parser.add_argument("--phase")
    run_heartbeat_parser.add_argument("--summary")
    run_heartbeat_parser.set_defaults(func=_cmd_run_heartbeat)
    run_finish_parser = run_subparsers.add_parser("finish")
    run_finish_parser.add_argument("--source", required=True)
    run_finish_parser.add_argument("--run-id", required=True)
    run_finish_parser.add_argument("--outcome", choices=RUN_OUTCOMES, required=True)
    run_finish_parser.add_argument("--summary")
    run_finish_parser.add_argument("--recovery")
    run_finish_parser.set_defaults(func=_cmd_run_finish)
    run_list_parser = run_subparsers.add_parser("list")
    run_list_parser.add_argument("--source", required=True)
    run_list_parser.add_argument("--recent", type=int, default=5)
    run_list_parser.add_argument("--stale-after", type=int, default=900)
    run_list_parser.add_argument("--missing-after", type=int, default=14400)
    run_list_parser.add_argument("--json", action="store_true")
    run_list_parser.set_defaults(func=_cmd_run_list)

    usage_parser = subparsers.add_parser("usage", help="Record and summarize plan usage")
    usage_subparsers = usage_parser.add_subparsers(dest="usage_command", required=True)
    usage_record_parser = usage_subparsers.add_parser("record", help="Record a usage snapshot")
    usage_record_parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    usage_record_parser.add_argument("--primary-used", type=float, required=True)
    usage_record_parser.add_argument("--secondary-used", type=float)
    usage_record_parser.add_argument("--primary-resets-at")
    usage_record_parser.add_argument("--secondary-resets-at")
    usage_record_parser.add_argument("--work-id")
    usage_record_parser.add_argument("--source", default="collector")
    usage_record_parser.set_defaults(func=_cmd_usage_record)
    usage_dashboard_parser = usage_subparsers.add_parser("dashboard", help="Show usage history")
    usage_dashboard_parser.add_argument("--hours", type=int, default=720)
    usage_dashboard_parser.set_defaults(func=_cmd_usage_dashboard)

    vm_parser = subparsers.add_parser("vm", help="Record and list fallback VM events")
    vm_subparsers = vm_parser.add_subparsers(dest="vm_command", required=True)
    vm_record_parser = vm_subparsers.add_parser("record", help="Record a VM lifecycle event")
    vm_record_parser.add_argument("--action", choices=("create", "delete"), required=True)
    vm_record_parser.add_argument("--result", choices=("success", "failed"), required=True)
    vm_record_parser.add_argument("--instance", required=True)
    vm_record_parser.add_argument("--project", required=True)
    vm_record_parser.add_argument("--zone", required=True)
    vm_record_parser.set_defaults(func=_cmd_vm_record)
    vm_list_parser = vm_subparsers.add_parser("list", help="List recent VM lifecycle events")
    vm_list_parser.add_argument("--recent", type=int, default=10)
    vm_list_parser.add_argument("--json", action="store_true")
    vm_list_parser.set_defaults(func=_cmd_vm_list)

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

    priority_digest_parser = subparsers.add_parser(
        "priority-digest",
        help="Push a LINE carousel of no-priority issues (JSON array via stdin)",
    )
    priority_digest_parser.set_defaults(func=_cmd_priority_digest)

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
