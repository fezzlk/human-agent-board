import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def board(tmp_path, monkeypatch):
    for key in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_NOTIFY_USER_ID", "KOBITO_PROJECT_ID",
                "KOBITO_SLACK_THREAD_TS", "HUMAN_AGENT_NOTIFICATION_COMMAND", "HUMAN_AGENT_NOTIFICATION_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUMAN_AGENT_BOARD_ROOT", str(tmp_path / "board"))
    import board as module
    return importlib.reload(module)


def request(board, project="project-a", **kwargs):
    return board.add_item("agent-to-user", "kobito", "plan_request", "計画", "変更内容",
                          related_links=["https://linear.app/test/issue/TEST-1"], project_id=project, **kwargs)


def test_explicit_project_scope_filters_queue_and_refuses_other_project_access(board, monkeypatch):
    a = request(board)
    b = request(board, "project-b")
    monkeypatch.setenv("KOBITO_PROJECT_ID", "project-a")
    assert [item["filename"] for item in board.list_items_full("agent-to-user")] == [a]
    with pytest.raises(ValueError): board.get_item(b)
    with pytest.raises(ValueError): board.complete_item(b)
    with pytest.raises(ValueError): request(board, "project-b")


def test_identical_dedupe_keys_in_different_projects_do_not_overwrite(board):
    a = request(board, dedupe_key="plan")
    b = request(board, "project-b", dedupe_key="plan")
    assert a != b
    assert len(board.list_items_full("agent-to-user")) == 2
    assert request(board, dedupe_key="plan") == a


def test_slack_approval_requires_current_plan_and_exact_project(board):
    filename = request(board)
    digest = board.decision_digest(board.get_item(filename))
    with pytest.raises(ValueError):
        board.respond_to_item(filename, "approval", via="Slack", project_id="project-b",
                              expected_digest=digest, response_id="attempt-b")
    with pytest.raises(ValueError):
        board.respond_to_item(filename, "approval", via="Slack", project_id="project-a",
                              expected_digest="old-plan", response_id="old")
    assert board.list_items("user-to-agent") == []
    assert board.get_item(filename)["body"] == "変更内容"


def test_approval_retries_do_not_recreate_work_even_after_consumption(board):
    filename = request(board, thread_ts="123.000001")
    digest = board.decision_digest(board.get_item(filename))
    response = board.respond_to_item(filename, "approval", via="Slack", project_id="project-a",
                                     expected_digest=digest, response_id="slack-one")
    item = board.get_item(response)
    assert item["project_id"] == "project-a"
    assert item["slack_thread_ts"] == "123.000001"
    assert "Slack経由" in item["title"]
    board.complete_item(response)
    assert board.respond_to_item(filename, "approval", via="Slack", project_id="project-a",
                                  expected_digest=digest, response_id="slack-one") == response
    assert board.list_items("user-to-agent") == []
    with pytest.raises(ValueError):
        board.respond_to_item(filename, "rejection", via="Slack", project_id="project-a",
                              expected_digest=digest, response_id="slack-one")


def test_letter_redelivery_is_idempotent_after_worker_consumes_it(board):
    args = ("user-to-agent", "user", "task", "置き手紙", "あとで調査")
    first = board.add_item(*args, project_id="project-a", external_id="event-one")
    board.complete_item(first)
    assert board.add_item(*args, project_id="project-a", external_id="event-one") == first
    assert board.list_items("user-to-agent") == []
    with pytest.raises(ValueError):
        board.add_item(*args, project_id="project-b", external_id="event-one")


def test_crash_before_external_receipt_does_not_duplicate_letter(board):
    args = ("user-to-agent", "user", "task", "置き手紙", "あとで調査")
    first = board.add_item(*args, project_id="project-a", external_id="event-one")
    receipt = board.board_root() / "state" / "external-items" / "event-one.json"
    receipt.unlink()
    assert board.add_item(*args, project_id="project-a", external_id="event-one") == first
    assert len(board.list_items("user-to-agent")) == 1


def test_notification_command_is_opt_in_shell_free_and_has_no_line_fallback(board, monkeypatch):
    monkeypatch.setenv("HUMAN_AGENT_NOTIFICATION_BACKEND", "command")
    monkeypatch.setenv("HUMAN_AGENT_NOTIFICATION_COMMAND", "python3 '/path with spaces/notify.py'")
    calls = []
    monkeypatch.setattr(board, "notify_line", lambda *args: pytest.fail("must not duplicate LINE notification"))
    def run(argv, **kwargs):
        calls.append((argv, json.loads(kwargs["input"])))
        assert "shell" not in kwargs
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(board.subprocess, "run", run)
    filename = request(board)
    assert calls[0][0] == ["python3", "/path with spaces/notify.py"]
    assert calls[0][1]["item"]["project_id"] == "project-a"
    assert board.get_item(filename)
    pending = board.pending_notifications()
    assert len(pending) == 1 and pending[0]["filename"] == filename
    board.complete_notification(pending[0]["id"])
    assert board.pending_notifications() == []


def test_successful_notification_leaves_no_retry_and_edited_plan_notifies_again(board, monkeypatch):
    monkeypatch.setenv("HUMAN_AGENT_NOTIFICATION_BACKEND", "command")
    monkeypatch.setenv("HUMAN_AGENT_NOTIFICATION_COMMAND", "notifier")
    sent = []
    def send(argv, **kwargs):
        sent.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(board.subprocess, "run", send)
    filename = request(board, dedupe_key="plan")
    assert request(board, dedupe_key="plan") == filename
    assert len(sent) == 2
    assert board.pending_notifications() == []


def test_environment_context_scopes_status_and_notification(board, monkeypatch):
    monkeypatch.setenv("KOBITO_PROJECT_ID", "project-a")
    monkeypatch.setenv("KOBITO_SLACK_THREAD_TS", "123.000001")
    sent = []
    monkeypatch.setattr(board, "notify_item", lambda item, filename: sent.append(item))
    board.set_status("kobito", "TEST-1", "researching", "調査", "確認中", notify=True)
    current, _ = board.list_statuses("kobito")
    assert current[0]["project_id"] == "project-a"
    assert sent[0]["slack_thread_ts"] == "123.000001"
    monkeypatch.setenv("KOBITO_PROJECT_ID", "project-b")
    assert board.list_statuses("kobito")[0] == []
    with pytest.raises(ValueError):
        board.set_status("kobito", "TEST-1", "implementing", "変更", "作業中")


def test_cli_letter_preserves_scope_and_origin(board, capsys):
    assert board.main(["add", "--direction", "user-to-agent", "--from", "user", "--type", "task",
                       "--title", "置き手紙", "--body", "あとで", "--project-id", "project-a",
                       "--external-id", "slack-letter", "--thread-ts", "123.000001"]) == 0
    filename = capsys.readouterr().out.strip()
    assert board.get_item(filename)["slack_thread_ts"] == "123.000001"
    assert board.main(["list", "--direction", "user-to-agent", "--project-id", "project-b", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert board.main(["list", "--direction", "user-to-agent", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert board.main(["list", "--direction", "user-to-agent", "--all-projects", "--json"]) == 0
    assert [item["filename"] for item in json.loads(capsys.readouterr().out)] == [filename]
