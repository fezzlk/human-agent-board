#!/usr/bin/env python3
"""CLI for human-agent-board: a git-backed queue for requests between a human
user and AI coding agents (Claude Code, Codex, etc.)."""

import argparse
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
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

_QUEUE_MUTEX = threading.RLock()
_QUEUE_LOCAL = threading.local()


def _project_id(value=None):
    context = os.environ.get("KOBITO_PROJECT_ID") or None
    if value and context and value != context:
        raise ValueError("project ID differs from execution context")
    value = value or context
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("invalid project ID")
    return value


def _thread_ts(value=None):
    value = value or os.environ.get("KOBITO_SLACK_THREAD_TS")
    if value and not re.fullmatch(r"[0-9]+\.[0-9]+", value):
        raise ValueError("invalid Slack thread timestamp")
    return value


def _queue_write(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        with _QUEUE_MUTEX:
            if getattr(_QUEUE_LOCAL, "active", False):
                return func(*args, **kwargs)
            lock_path = board_root() / "state" / ".queue.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                _QUEUE_LOCAL.active = True
                try:
                    return func(*args, **kwargs)
                finally:
                    _QUEUE_LOCAL.active = False
                    fcntl.flock(lock, fcntl.LOCK_UN)
    return wrapped


def decision_digest(item):
    values = {key: item.get(key) for key in (
        "project_id", "type", "title", "body", "related_links", "created_at", "slack_thread_ts"
    )}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), encoding="utf-8", delete=False) as output:
        temp = Path(output.name)
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def notify_item(item, filename):
    backend = os.environ.get("HUMAN_AGENT_NOTIFICATION_BACKEND", "command")
    if backend == "none":
        return
    if backend != "command":
        print("notification backend is invalid", file=sys.stderr)
        return
    payload = json.dumps({"item": item, "filename": filename}, ensure_ascii=False)
    notification_id = hashlib.sha256(payload.encode()).hexdigest()
    pending = board_root() / "state" / "notifications" / (notification_id + ".json")
    _atomic_text(pending, payload)
    command = os.environ.get("HUMAN_AGENT_NOTIFICATION_COMMAND", "")
    if not command:
        # Kept in the outbox; the Slack listener delivers it on its next sync.
        print("notification command is not configured; notification is pending", file=sys.stderr)
        return
    try:
        completed = subprocess.run(
            shlex.split(command),
            input=payload,
            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        if completed.returncode:
            print("notification command failed; board item is retained", file=sys.stderr)
        else:
            pending.unlink(missing_ok=True)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("notification command unavailable; board item is retained", file=sys.stderr)


def pending_notifications():
    directory = board_root() / "state" / "notifications"
    result = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        result.append({"id": path.stem, **payload})
    return result


def complete_notification(notification_id):
    if not re.fullmatch(r"[0-9a-f]{64}", notification_id):
        raise ValueError("invalid notification ID")
    (board_root() / "state" / "notifications" / (notification_id + ".json")).unlink(missing_ok=True)


def _related_link_label(url, index):
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    if hostname == "github.com" or hostname.endswith(".github.com"):
        return "GitHub"
    if hostname == "linear.app" or hostname.endswith(".linear.app"):
        return "Linear"
    return f"関連資料 {index}"


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
    _atomic_text(path, yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
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


@_queue_write
def set_status(source, work_id, state, title, summary, next_action=None,
               related_links=None, notify=False, project_id=None, thread_ts=None):
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
    project_id = _project_id(project_id)
    if project_id:
        item["project_id"] = project_id
    thread_ts = _thread_ts(thread_ts)
    if thread_ts:
        item["slack_thread_ts"] = thread_ts
    if next_action:
        item["next_action"] = next_action
    if related_links:
        item["related_links"] = list(related_links)

    current_path = _status_path(source, work_id)
    previous = []
    if current_path.exists():
        with current_path.open(encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
        if existing.get("project_id") != project_id:
            raise ValueError("current work status belongs to a different project scope")
        previous.extend(_load_status_files(current_path.parent))
    previous.extend(_load_status_files(status_history_dir()))
    previous_state = next(
        (
            entry.get("state")
            for entry in previous
            if entry.get("source") == source and entry.get("work_id") == work_id
            and entry.get("project_id") == project_id
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
    # state changes. Reworded copies of the same failure should not spam notifications.
    if notify and previous_state != state:
        notify_item(
            {
                "type": "status_update",
                "title": f"{work_id}: {title}",
                "body": f"[{state}] {summary}",
                "related_links": item.get("related_links", []),
                **({"project_id": project_id} if project_id else {}),
                **({"slack_thread_ts": thread_ts} if thread_ts else {}),
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


def list_statuses(source=None, recent=5, project_id=None):
    current = _load_status_files(status_current_dir())
    history = _load_status_files(status_history_dir())
    project_id = _project_id(project_id)
    if project_id:
        current = [item for item in current if item.get("project_id") == project_id]
        history = [item for item in history if item.get("project_id") == project_id]
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


@_queue_write
def add_item(direction, from_, type_, title, body, related_links=None, dedupe_key=None,
             project_id=None, external_id=None, thread_ts=None):
    project_id = _project_id(project_id)
    thread_ts = _thread_ts(thread_ts)
    if external_id:
        external_id = _safe_status_key(external_id, "external_id")
        receipt = board_root() / "state" / "external-items" / (external_id + ".json")
        if receipt.exists():
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            if saved.get("project_id") != project_id or saved.get("direction") != direction:
                raise ValueError("external event belongs to another scope")
            return saved["filename"]
        # Recover a crash after the YAML commit but before its receipt.
        existing = next((item for item in list_items_full(direction, project_id=project_id)
                         if item.get("external_id") == external_id), None)
        if existing:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            _atomic_text(receipt, json.dumps({
                "project_id": project_id, "direction": direction, "filename": existing["filename"],
            }))
            return existing["filename"]
    directory = direction_dir(direction)
    directory.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    if dedupe_key:
        dedupe_key = _safe_status_key(dedupe_key, "dedupe_key")
        existing = next(
            (item for item in list_items_full(direction, project_id=project_id)
             if item.get("project_id") == project_id and item.get("dedupe_key") == dedupe_key),
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
            _atomic_text(path, yaml.safe_dump(existing, allow_unicode=True, sort_keys=False))
            if direction == "agent-to-user" and os.environ.get("HUMAN_AGENT_NOTIFICATION_BACKEND", "command") == "command":
                notify_item(existing, path.name)
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
    if project_id:
        item["project_id"] = project_id
    if thread_ts:
        item["slack_thread_ts"] = thread_ts
    if external_id:
        item["external_id"] = external_id
    if related_links:
        item["related_links"] = list(related_links)
    if dedupe_key:
        item["dedupe_key"] = dedupe_key

    _atomic_text(path, yaml.safe_dump(item, allow_unicode=True, sort_keys=False))

    if external_id:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        _atomic_text(receipt, json.dumps({
            "project_id": project_id, "direction": direction, "filename": filename,
        }))

    if direction == "agent-to-user":
        notify_item(item, filename)

    return filename


@_queue_write
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


def get_item(filename, direction=None, project_id=None):
    filename = _safe_item_filename(filename)
    project_id = _project_id(project_id)
    directions = (direction,) if direction else DIRECTIONS
    for candidate_direction in directions:
        if candidate_direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of: {', '.join(DIRECTIONS)}")
        path = direction_dir(candidate_direction) / filename
        if path.exists():
            with path.open(encoding="utf-8") as f:
                item = yaml.safe_load(f) or {}
            if project_id and item.get("project_id") != project_id:
                raise ValueError("board item belongs to another project")
            item["filename"] = filename
            item["direction"] = candidate_direction
            return item
    raise FileNotFoundError(f"{filename} not found")


REQUIRED_ITEM_KEYS = ("from", "type", "title")


def _load_queue_item(path):
    """Load one queue file, or return None (with a warning) if it is malformed.

    A single broken or foreign-format file must not make the whole queue
    unreadable, so listing skips it and reports it on stderr instead.
    """
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        print(f"warning: skipping unreadable board item {path.name}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"warning: skipping malformed board item {path.name}: not a mapping", file=sys.stderr)
        return None
    missing = [key for key in REQUIRED_ITEM_KEYS if key not in data]
    if missing:
        print(
            f"warning: skipping malformed board item {path.name}: missing {', '.join(missing)}",
            file=sys.stderr,
        )
        return None
    return data


def list_items_full(direction, project_id=None):
    project_id = _project_id(project_id)
    directory = direction_dir(direction)
    if not directory.exists():
        return []

    items = []
    for path in directory.glob("*.yaml"):
        data = _load_queue_item(path)
        if data is None:
            continue
        if project_id and data.get("project_id") != project_id:
            continue
        item = dict(data)
        item["filename"] = path.name
        item["direction"] = direction
        items.append(item)
    items.sort(key=lambda item: item["filename"])
    return items


def list_items(direction, project_id=None):
    return [
        {
            "filename": item["filename"],
            "from": item.get("from"),
            "type": item.get("type"),
            "title": item.get("title"),
            "created_at": item.get("created_at"),
            **({"project_id": item["project_id"]} if item.get("project_id") else {}),
        }
        for item in list_items_full(direction, project_id=project_id)
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


@_queue_write
def respond_to_item(filename, decision, via="Slack", project_id=None,
                    expected_digest=None, response_id=None):
    if decision not in ("approval", "rejection"):
        raise ValueError("decision must be approval or rejection")
    project_id = _project_id(project_id)
    if via != "Slack":
        raise ValueError("invalid response channel")
    if not expected_digest or not response_id:
        raise ValueError("Slack response requires digest and response ID")
    if response_id:
        response_id = _safe_status_key(response_id, "response_id")
        receipt = board_root() / "state" / "responses" / (response_id + ".json")
        if receipt.exists():
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            if (saved["project_id"], saved["request"], saved["decision"], saved["digest"]) != (
                project_id, filename, decision, expected_digest
            ):
                raise ValueError("response ID already used for a different decision")
            try:
                pending = get_item(filename, direction="agent-to-user", project_id=project_id)
                if decision_digest(pending) == expected_digest:
                    complete_item(filename)
            except FileNotFoundError:
                pass
            return saved["response"]
    item = get_item(filename, direction="agent-to-user", project_id=project_id)
    if not project_id and item.get("project_id"):
        # Without a project scope, only items that belong to no project can be answered.
        raise ValueError("board item belongs to a project")
    if item.get("type") not in DECISION_TYPES:
        raise ValueError("board item does not require a decision")
    if expected_digest and decision_digest(item) != expected_digest:
        raise ValueError("decision changed; review the current plan before approving")

    label = "承認" if decision == "approval" else "却下"
    response_filename = add_item(
        direction="user-to-agent",
        from_="user",
        type_=decision,
        title=f"{label} ({via}経由): {item.get('title', '')}",
        body=f"{via} Boardから{label}されました。",
        related_links=item.get("related_links"),
        project_id=item.get("project_id"),
        external_id=("response-" + response_id) if response_id else None,
        thread_ts=item.get("slack_thread_ts"),
    )
    if response_id:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        _atomic_text(receipt, json.dumps({
            "project_id": project_id, "request": filename, "decision": decision,
            "digest": expected_digest, "response": response_filename,
        }))
    complete_item(filename)
    return response_filename


@_queue_write
def complete_item(filename):
    filename = _safe_item_filename(filename)
    for direction in DIRECTIONS:
        path = direction_dir(direction) / filename
        if path.exists():
            get_item(filename, direction=direction)
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
        project_id=args.project_id,
        external_id=args.external_id,
        thread_ts=args.thread_ts,
    )
    print(filename)


def _cmd_list(args):
    items = list_items_full(args.direction, project_id=args.project_id)
    if not _project_id(args.project_id) and not args.all_projects:
        # Existing global workers must not consume a project's Slack letters.
        items = [item for item in items if not item.get("project_id")]
    if args.json:
        print(json.dumps(items, ensure_ascii=False))
        return
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
    print(respond_to_item(
        args.filename, args.decision, via=args.via, project_id=args.project_id,
        expected_digest=args.expected_digest, response_id=args.response_id,
    ))


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
        project_id=args.project_id,
        thread_ts=args.thread_ts,
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
    add_parser.add_argument("--dedupe-key")
    add_parser.add_argument("--project-id")
    add_parser.add_argument("--external-id")
    add_parser.add_argument("--thread-ts")
    add_parser.set_defaults(func=_cmd_add)

    list_parser = subparsers.add_parser("list", help="List pending items")
    list_parser.add_argument("--direction", choices=DIRECTIONS, required=True)
    list_parser.add_argument("--project-id")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--all-projects", action="store_true", help="Explicit global read-only overview")
    list_parser.set_defaults(func=_cmd_list)

    notifications = subparsers.add_parser("notifications", help="Read durable command notifications")
    notifications.set_defaults(func=lambda args: print(json.dumps(pending_notifications(), ensure_ascii=False)))
    notification_complete = subparsers.add_parser("notification-complete", help="Acknowledge delivered notification")
    notification_complete.add_argument("notification_id")
    notification_complete.set_defaults(func=lambda args: complete_notification(args.notification_id))

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
    respond_parser.add_argument("--via", choices=("Slack",), default="Slack")
    respond_parser.add_argument("--project-id")
    respond_parser.add_argument("--expected-digest")
    respond_parser.add_argument("--response-id")
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
    status_set_parser.add_argument("--project-id")
    status_set_parser.add_argument("--thread-ts")
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
