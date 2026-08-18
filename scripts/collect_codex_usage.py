#!/usr/bin/env python3
"""Read Codex plan rate limits from the official local app-server protocol."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import board  # noqa: E402


def request(process, request_id, method, params):
    message = {"id": request_id, "method": method, "params": params}
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    for line in process.stdout:
        response = json.loads(line)
        if response.get("id") == request_id:
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            return response.get("result") or {}
    raise RuntimeError("Codex app-server exited without a response")


def collect():
    process = subprocess.Popen(
        [os.environ.get("CODEX_BIN", "codex"), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        request(process, 1, "initialize", {
            "clientInfo": {"name": "human-agent-board", "version": "1"}
        })
        result = request(process, 2, "account/rateLimits/read", None)
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    snapshot = result.get("rateLimits") or {}
    primary = snapshot.get("primary") or {}
    secondary = snapshot.get("secondary") or {}
    if primary.get("usedPercent") is None:
        raise RuntimeError("Codex rate-limit response did not include a primary window")
    return board.record_usage(
        "codex",
        primary["usedPercent"],
        secondary.get("usedPercent"),
        primary.get("resetsAt"),
        secondary.get("resetsAt"),
        os.environ.get("HUMAN_AGENT_WORK_ID"),
        source="codex-app-server",
    )


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False))
