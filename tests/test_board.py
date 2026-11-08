import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def board(tmp_path, monkeypatch):
    # Never inherit the real notifier. Tests that verify delivery patch notify_item.
    for key in ("HUMAN_AGENT_NOTIFICATION_BACKEND", "HUMAN_AGENT_NOTIFICATION_COMMAND", "KOBITO_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
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


def test_list_skips_malformed_items_and_warns(board, capsys):
    filename = board.add_item(
        direction="agent-to-user",
        from_="claude",
        type_="approval_request",
        title="Deploy to prod?",
        body="details here",
    )
    directory = board.direction_dir("agent-to-user")
    # A status-shaped record without from/type, a non-mapping, and broken YAML.
    (directory / "20200101T000000Z_aaaaaa.yaml").write_text(
        "source: user\nwork_id: X-1\nstate: rejected\ntitle: misplaced\n", encoding="utf-8"
    )
    (directory / "20200101T000001Z_bbbbbb.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    (directory / "20200101T000002Z_cccccc.yaml").write_text("from: [unclosed\n", encoding="utf-8")

    items = board.list_items("agent-to-user")
    dashboard = board.dashboard_data()

    assert [item["filename"] for item in items] == [filename]
    assert [item["filename"] for item in dashboard["decisions"] + dashboard["notifications"]] == [filename]
    err = capsys.readouterr().err
    assert "20200101T000000Z_aaaaaa.yaml: missing from, type" in err
    assert "20200101T000001Z_bbbbbb.yaml: not a mapping" in err
    assert "20200101T000002Z_cccccc.yaml" in err


def test_cli_list_does_not_crash_on_malformed_item(board, capsys):
    board.add_item(
        direction="agent-to-user", from_="claude", type_="fyi", title="ok", body="b",
    )
    (board.direction_dir("agent-to-user") / "20200101T000000Z_aaaaaa.yaml").write_text(
        "title: no sender\n", encoding="utf-8"
    )

    assert board.main(["list", "--direction", "agent-to-user"]) == 0

    captured = capsys.readouterr()
    assert "\tclaude\tfyi\tok" in captured.out
    assert "missing from, type" in captured.err


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


def test_complete_rejects_path_traversal(board):
    with pytest.raises(ValueError):
        board.complete_item("../outside.yaml")


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


def test_get_item_returns_full_item(board):
    filename = board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="decision_request",
        title="choose approach",
        body="full details",
        related_links=["https://linear.app/example/issue/FEZ-112/example"],
    )

    item = board.get_item(filename)

    assert item["filename"] == filename
    assert item["direction"] == "agent-to-user"
    assert item["body"] == "full details"
    assert item["related_links"] == [
        "https://linear.app/example/issue/FEZ-112/example"
    ]


def test_respond_to_item_records_response_and_completes_request(board):
    filename = board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="approval_request",
        title="deploy?",
        body="please decide",
        related_links=["https://github.com/example/repo/pull/1"],
    )

    digest = board.decision_digest(board.get_item(filename))
    response_filename = board.respond_to_item(filename, "approval", expected_digest=digest, response_id="r1")

    with pytest.raises(FileNotFoundError):
        board.get_item(filename)
    response = board.get_item(response_filename, direction="user-to-agent")
    assert response["type"] == "approval"
    assert response["title"] == "承認 (Slack経由): deploy?"
    assert response["related_links"] == ["https://github.com/example/repo/pull/1"]


def test_respond_rejects_non_decision_item(board):
    filename = board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="fyi",
        title="done",
        body="just information",
    )

    with pytest.raises(ValueError):
        board.respond_to_item(filename, "approval", expected_digest="x", response_id="r1")


def test_respond_without_project_only_answers_items_without_project(board):
    scoped = board.add_item("agent-to-user", "kobito", "plan_request", "plan", "body", project_id="project-a")
    digest = board.decision_digest(board.get_item(scoped))
    with pytest.raises(ValueError, match="belongs to a project"):
        board.respond_to_item(scoped, "approval", expected_digest=digest, response_id="r1")
    assert board.get_item(scoped)


def test_respond_requires_digest_and_response_id(board):
    filename = board.add_item("agent-to-user", "kobito", "plan_request", "plan", "body")
    with pytest.raises(ValueError, match="digest and response ID"):
        board.respond_to_item(filename, "approval")


def test_dashboard_data_groups_board_items_and_status(board):
    board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="decision_request",
        title="decision",
        body="choose",
    )
    board.add_item(
        direction="agent-to-user",
        from_="kobito",
        type_="fyi",
        title="notice",
        body="done",
    )
    board.add_item(
        direction="user-to-agent",
        from_="user",
        type_="task",
        title="request",
        body="please work",
    )
    board.set_status(
        source="kobito",
        work_id="FEZ-112",
        state="implementing",
        title="dashboard",
        summary="building it",
    )

    data = board.dashboard_data()

    assert [item["title"] for item in data["decisions"]] == ["decision"]
    assert [item["title"] for item in data["notifications"]] == ["notice"]
    assert [item["title"] for item in data["user_requests"]] == ["request"]
    assert data["status_current"][0]["work_id"] == "FEZ-112"
    assert data["kobito_run"]["health"] == "missing"


def test_run_lifecycle_reports_healthy_and_keeps_history(board):
    started = board.start_run(
        "kobito", trigger="cloud-scheduler", run_id="run-1",
        started_at="2026-08-18T03:00:00Z",
    )
    assert started["state"] == "running"
    board.heartbeat_run(
        "kobito", "run-1", phase="working", summary="checking issues",
        recorded_at="2026-08-18T03:01:00Z",
    )
    board.finish_run(
        "kobito", "run-1", "completed", summary="pass completed",
        finished_at="2026-08-18T03:02:00Z",
    )

    health = board.run_health(
        "kobito", now=board.datetime.fromisoformat("2026-08-18T03:03:00+00:00").timestamp()
    )

    assert health["health"] == "healthy"
    assert health["active"] == []
    assert health["latest"]["summary"] == "pass completed"


def test_run_health_detects_stale_and_consecutive_failures(board):
    board.start_run(
        "kobito", run_id="stuck", started_at="2026-08-18T00:00:00Z"
    )
    for index in range(2):
        run_id = f"failed-{index}"
        board.start_run(
            "kobito", run_id=run_id,
            started_at=f"2026-08-17T0{index}:00:00Z",
        )
        board.finish_run(
            "kobito", run_id, "failed", finished_at=f"2026-08-17T0{index}:01:00Z"
        )

    health = board.run_health(
        "kobito", stale_after=900, missing_after=14400,
        now=board.datetime.fromisoformat("2026-08-18T01:00:00+00:00").timestamp(),
    )

    assert health["health"] == "stale"
    assert health["active"][0]["stale"] is True
    assert health["consecutive_failures"] == 2


def test_add_dedupe_key_updates_one_item_and_resolve_removes_it(board, monkeypatch):
    notifications = []
    monkeypatch.setattr(board, "notify_item", lambda item, filename: notifications.append(filename))

    first = board.add_item(
        "agent-to-user", "kobito", "action_required", "Fix auth", "first",
        dedupe_key="connectivity-preflight",
    )
    second = board.add_item(
        "agent-to-user", "kobito", "action_required", "Fix auth", "second",
        dedupe_key="connectivity-preflight",
    )

    assert first == second
    assert len(board.list_items_full("agent-to-user")) == 1
    assert board.get_item(first)["body"] == "second"
    # The Slack notifier deduplicates by content, so an updated body is delivered again.
    assert notifications == [first, first]
    assert board.resolve_item("agent-to-user", "connectivity-preflight") == 1
    assert board.list_items_full("agent-to-user") == []


def test_record_and_list_vm_events_newest_first(board):
    board.record_vm_event(
        action="create", result="success", instance="vm-one",
        project="sample-project", zone="asia-northeast1-a",
        recorded_at="2026-08-18T05:00:00Z",
    )
    board.record_vm_event(
        action="delete", result="failed", instance="vm-one",
        project="sample-project", zone="asia-northeast1-a",
        recorded_at="2026-08-18T06:00:00Z",
    )

    events = board.list_vm_events(recent=1)

    assert events == [{
        "action": "delete",
        "result": "failed",
        "instance": "vm-one",
        "project": "sample-project",
        "zone": "asia-northeast1-a",
        "recorded_at": "2026-08-18T06:00:00Z",
    }]


def test_record_vm_event_rejects_unknown_values(board):
    with pytest.raises(ValueError):
        board.record_vm_event("start", "success", "vm", "project", "zone")
    with pytest.raises(ValueError):
        board.record_vm_event("create", "unknown", "vm", "project", "zone")


def test_status_notification_has_links_without_decision_buttons(board, monkeypatch):
    calls = []
    monkeypatch.setattr(board, "notify_item", lambda item, filename: calls.append(item))

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

    assert calls[0]["type"] not in board.DECISION_TYPES
    assert "[pr_open] PR is ready" in calls[0]["body"]
    assert calls[0]["related_links"] == [
        "https://github.com/fezzlk/human-agent-board/pull/2",
        "https://linear.app/fezzlk/issue/FEZ-111/example",
    ]


def test_repeated_status_state_does_not_notify_twice(board, monkeypatch):
    calls = []
    monkeypatch.setattr(board, "notify_item", lambda item, filename: calls.append(item))

    for summary in ("first details", "same failure, reworded"):
        board.set_status(
            source="kobito",
            work_id="connectivity-preflight",
            state="failed",
            title="preflight",
            summary=summary,
            notify=True,
        )

    assert len(calls) == 1


def test_notification_without_command_stays_pending(board, capsys):
    filename = board.add_item("agent-to-user", "kobito", "action_required", "kobito障害", "対処: 再起動")

    pending = board.pending_notifications()
    assert [entry["filename"] for entry in pending] == [filename]
    assert "pending" in capsys.readouterr().err


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


def test_record_and_dashboard_usage(board):
    board.record_usage(
        "codex", 20, 40, "reset-a", "reset-b", "FEZ-113",
        recorded_at="2026-08-18T10:00:00Z",
    )
    board.record_usage(
        "codex", 27, 43, "reset-a", "reset-b", "FEZ-113",
        recorded_at="2026-08-18T11:00:00Z",
    )

    dashboard = board.usage_dashboard(hours=100000)

    assert dashboard["latest"]["codex"]["primary_used"] == 27
    assert dashboard["tasks"][0]["primary_used_delta"] == 7
    assert dashboard["savings"]["status"] == "insufficient_data"


def test_record_usage_rejects_invalid_percentage(board):
    with pytest.raises(ValueError, match="between 0 and 100"):
        board.record_usage("claude", 101)