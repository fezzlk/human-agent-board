#!/usr/bin/env python3
"""Install Claude statusline and a 15-minute Codex usage collector on macOS."""

import json
import plistlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.fezzlk.human-agent-board-usage.plist"


def main():
    settings = {}
    if CLAUDE_SETTINGS.exists():
        settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    settings["statusLine"] = {
        "type": "command",
        "command": str(REPO / "scripts" / "claude_usage_statusline.py"),
    }
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    payload = {
        "Label": "com.fezzlk.human-agent-board-usage",
        "ProgramArguments": [sys.executable, str(REPO / "scripts" / "collect_codex_usage.py")],
        "WorkingDirectory": str(REPO),
        "StartInterval": 900,
        "RunAtLoad": True,
        "EnvironmentVariables": {
            "CODEX_BIN": "/Applications/ChatGPT.app/Contents/Resources/codex",
        },
        "StandardOutPath": "/tmp/human-agent-board-usage.log",
        "StandardErrorPath": "/tmp/human-agent-board-usage-error.log",
    }
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_AGENT.open("wb") as f:
        plistlib.dump(payload, f)
    print(f"updated {CLAUDE_SETTINGS}")
    print(f"created {LAUNCH_AGENT}")


if __name__ == "__main__":
    main()
