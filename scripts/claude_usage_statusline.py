#!/usr/bin/env python3
"""Record Claude Code rate-limit data received through the statusline input."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import board  # noqa: E402


def window(rate_limits, *names):
    for name in names:
        value = rate_limits.get(name)
        if isinstance(value, dict):
            return value
    return {}


def used_percent(value):
    result = value.get("used_percentage", value.get("usedPercent"))
    return float(result) if result is not None else None


def main():
    payload = json.load(sys.stdin)
    limits = payload.get("rate_limits") or {}
    primary = window(limits, "five_hour", "fiveHour", "primary")
    secondary = window(limits, "seven_day", "sevenDay", "secondary")
    primary_used = used_percent(primary)
    secondary_used = used_percent(secondary)
    if primary_used is not None:
        board.record_usage(
            "claude",
            primary_used,
            secondary_used,
            primary.get("resets_at", primary.get("resetsAt")),
            secondary.get("resets_at", secondary.get("resetsAt")),
            os.environ.get("HUMAN_AGENT_WORK_ID"),
            source="claude-statusline",
        )
    parts = []
    if primary_used is not None:
        parts.append(f"5h {100 - primary_used:.0f}%残")
    if secondary_used is not None:
        parts.append(f"7d {100 - secondary_used:.0f}%残")
    print("Claude " + " · ".join(parts) if parts else "Claude 利用量取得待ち")


if __name__ == "__main__":
    main()
