import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HUMAN_AGENT_BOARD_ROOT", str(tmp_path / "board"))
    import board as board_module

    importlib.reload(board_module)
    return board_module


def test_add_then_list(board):
    filename = board.add_item(
        direction="agent-to-user",
        from_="claude",
        type_="approval_request",
        title="Deploy to prod?",
        body="details here",
    )

    items = board.list_items("agent-to-user")

    assert len(items) == 1
    assert items[0]["filename"] == filename
    assert items[0]["from"] == "claude"
    assert items[0]["type"] == "approval_request"
    assert items[0]["title"] == "Deploy to prod?"


def test_list_empty_direction_returns_empty(board):
    assert board.list_items("user-to-agent") == []


def test_complete_removes_item(board):
    filename = board.add_item(
        direction="user-to-agent",
        from_="user",
        type_="task",
        title="do the thing",
        body="details",
    )

    direction = board.complete_item(filename)

    assert direction == "user-to-agent"
    assert board.list_items("user-to-agent") == []


def test_complete_missing_file_raises(board):
    with pytest.raises(FileNotFoundError):
        board.complete_item("does-not-exist.yaml")


def test_add_with_related_links(board):
    filename = board.add_item(
        direction="agent-to-user",
        from_="codex",
        type_="fyi",
        title="heads up",
        body="details",
        related_links=["https://example.com/a", "https://example.com/b"],
    )

    path = board.direction_dir("agent-to-user") / filename
    content = path.read_text(encoding="utf-8")

    assert "https://example.com/a" in content
    assert "https://example.com/b" in content


def test_notify_line_noop_without_env(board, monkeypatch):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_NOTIFY_USER_ID", raising=False)
    calls = []
    monkeypatch.setattr(
        board.urllib.request, "urlopen", lambda *a, **k: calls.append((a, k))
    )

    board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="plan_request",
        title="do the thing",
        body="details",
        related_links=["https://example.com/issue"],
    )

    assert calls == []


def test_notify_line_pushes_buttons_and_related_links_when_configured(board, monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_NOTIFY_USER_ID", "U1234")
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()

    monkeypatch.setattr(board.urllib.request, "urlopen", fake_urlopen)

    board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="plan_request",
        title="do the thing",
        body="details",
        related_links=[
            "https://github.com/fezzlk/human-agent-board/pull/1",
            "https://linear.app/fezzlk/issue/FEZ-110/example",
            "https://example.com/design",
        ],
    )

    assert len(calls) == 1
    payload = json.loads(calls[0].data)
    assert payload["to"] == "U1234"
    assert len(payload["messages"]) == 2
    template = payload["messages"][0]["template"]
    assert template["actions"][0]["data"] == (
        "approve|https://github.com/fezzlk/human-agent-board/pull/1"
    )
    assert template["actions"][1]["data"] == (
        "reject|https://github.com/fezzlk/human-agent-board/pull/1"
    )
    details = payload["messages"][1]
    assert details["type"] == "text"
    assert "GitHub: https://github.com/fezzlk/human-agent-board/pull/1" in details["text"]
    assert "Linear: https://linear.app/fezzlk/issue/FEZ-110/example" in details["text"]
    assert "関連資料 3: https://example.com/design" in details["text"]


def test_notify_line_without_related_links_sends_text_only(board, monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_NOTIFY_USER_ID", "U1234")
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)

    monkeypatch.setattr(board.urllib.request, "urlopen", fake_urlopen)

    board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="fyi",
        title="status",
        body="no approval required",
    )

    payload = json.loads(calls[0].data)
    assert payload["messages"] == [{"type": "text", "text": "status\nno approval required"}]


def test_status_notification_has_links_without_decision_buttons(board, monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_NOTIFY_USER_ID", "U1234")
    calls = []
    monkeypatch.setattr(
        board.urllib.request, "urlopen", lambda request, timeout=None: calls.append(request)
    )

    board.set_status(
        source="kobito",
        work_id="FEZ-111",
        state="pr_open",
        title="status feature",
        summary="PR is ready",
        related_links=[
            "https://github.com/fezzlk/human-agent-board/pull/2",
            "https://linear.app/fezzlk/issue/FEZ-111/example",
        ],
        notify=True,
    )

    payload = json.loads(calls[0].data)
    assert [message["type"] for message in payload["messages"]] == ["text", "text"]
    assert "[pr_open] PR is ready" in payload["messages"][0]["text"]
    assert "GitHub:" in payload["messages"][1]["text"]
    assert "Linear:" in payload["messages"][1]["text"]


def test_set_status_updates_current_snapshot(board):
    board.set_status(
        source="kobito",
        work_id="FEZ-111",
        state="researching",
        title="status feature",
        summary="reading the repositories",
    )
    board.set_status(
        source="kobito",
        work_id="FEZ-111",
        state="implementing",
        title="status feature",
        summary="adding status commands",
        next_action="run tests",
        related_links=["https://linear.app/fezzlk/issue/FEZ-111/example"],
    )

    current, history = board.list_statuses(source="kobito")
    assert len(current) == 1
    assert history == []
    assert current[0]["state"] == "implementing"
    assert current[0]["summary"] == "adding status commands"
    assert current[0]["next_action"] == "run tests"


def test_terminal_status_moves_snapshot_to_history(board):
    board.set_status(
        source="kobito",
        work_id="FEZ-111",
        state="verifying",
        title="status feature",
        summary="running tests",
    )
    board.set_status(
        source="kobito",
        work_id="FEZ-111",
        state="completed",
        title="status feature",
        summary="all tests passed",
        related_links=["https://github.com/fezzlk/human-agent-board/pull/2"],
    )

    current, history = board.list_statuses(source="kobito")
    assert current == []
    assert len(history) == 1
    assert history[0]["state"] == "completed"
    assert "FEZ-111" in board.format_status_list(source="kobito")


def test_status_key_rejects_path_traversal(board):
    with pytest.raises(ValueError):
        board.set_status(
            source="kobito",
            work_id="../escape",
            state="waiting",
            title="bad",
            summary="bad",
        )


def test_notify_line_failure_does_not_raise(board, monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_NOTIFY_USER_ID", "U1234")

    def raise_error(*a, **k):
        raise board.urllib.error.URLError("boom")

    monkeypatch.setattr(board.urllib.request, "urlopen", raise_error)

    filename = board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="plan_request",
        title="do the thing",
        body="details",
        related_links=["https://example.com/issue"],
    )

    assert board.list_items("agent-to-user")[0]["filename"] == filename
