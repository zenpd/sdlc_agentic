import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from backend import manual_implementation
from backend.jira_bot import PipelineConfig


def test_agent_retries_once_when_it_finishes_without_repository_changes(monkeypatch):
    class Conversation:
        def __init__(self):
            self.messages = []
            self.state = SimpleNamespace(
                execution_status=SimpleNamespace(value="finished"),
                active_branch=lambda: "branch",
            )

        def send_message(self, message):
            self.messages.append(message)

        def run(self):
            pass

    conversation = Conversation()
    snapshot = {"changed_files": ["src/app.py"], "proposed_files": [], "proposed_patch": "diff"}
    review_results = iter([
        RuntimeError("The reviewed implementation contains no changes"),
        snapshot,
    ])
    monkeypatch.setattr(
        manual_implementation,
        "review_snapshot",
        lambda *_args: result_or_raise(next(review_results)),
    )

    summary, proposal = manual_implementation._run_agent_for_review(
        conversation,
        "Implement the ticket.",
        Path("."),
        "base",
        lambda _branch: "Implemented the ticket.",
        lambda: False,
    )

    assert summary == "Implemented the ticket."
    assert proposal == snapshot
    assert len(conversation.messages) == 2
    assert "without changing any repository files" in conversation.messages[1]


def test_agent_reports_actionable_error_after_retry_also_makes_no_changes(
    monkeypatch,
):
    class Conversation:
        def __init__(self):
            self.state = SimpleNamespace(
                execution_status=SimpleNamespace(value="finished"),
                active_branch=lambda: "branch",
            )

        def send_message(self, _message):
            pass

        def run(self):
            pass

    monkeypatch.setattr(
        manual_implementation,
        "review_snapshot",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("The reviewed implementation contains no changes")
        ),
    )

    with pytest.raises(RuntimeError, match="completed twice without changing"):
        manual_implementation._run_agent_for_review(
            Conversation(),
            "Implement the ticket.",
            Path("."),
            "base",
            lambda _branch: "No changes made.",
            lambda: False,
        )


def result_or_raise(result):
    if isinstance(result, Exception):
        raise result
    return result


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def implementation_workspace(tmp_path, monkeypatch):
    run_id = "674cb0b1-8c51-48ca-8d84-85fbbfa39000"
    workspace = tmp_path / "workspaces" / run_id
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    source = repo / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("VALUE = 'original'\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "initial")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feat/674-human-reviewed")
    monkeypatch.setattr(
        manual_implementation,
        "manual_workspace_path",
        lambda _run_id: workspace,
    )
    config = PipelineConfig(
        target_repo="owner/repo",
        target_branch="main",
        verify_mode="command",
        test_command="configured-tests",
        test_timeout_min=1,
    )
    task_data = {
        "branch": "feat/674-human-reviewed",
        "base_sha": base_sha,
        "summary": "Implement approved behavior",
        "agent_summary": "Added the behavior.",
        "proposed_files": [
            {
                "path": "src/app.py",
                "content": "VALUE = 'agent'\n",
                "deleted": False,
            }
        ],
    }
    return run_id, workspace, repo, source, config, task_data


def test_validation_runs_on_human_edited_files_and_records_final_commit(
    implementation_workspace,
    monkeypatch,
):
    run_id, _workspace, repo, source, config, task_data = implementation_workspace
    human_code = "VALUE = 'human-approved'\n"
    manual_implementation.apply_reviewed_files(
        run_id,
        config,
        task_data,
        {"src/app.py": human_code},
    )
    monkeypatch.setattr(
        manual_implementation,
        "run_shell",
        lambda command, cwd, _timeout: (
            0
            if command == "configured-tests"
            and cwd == repo
            and source.read_text(encoding="utf-8") == human_code
            else 1,
            "configured checks passed",
        ),
    )

    result = manual_implementation.validate_and_commit(run_id, config, task_data)

    assert source.read_text(encoding="utf-8") == human_code
    assert result["branch"] == "feat/674-human-reviewed"
    assert result["commit_sha"] == _git(repo, "rev-parse", "HEAD")
    assert result["changed_files"] == "src/app.py"
    assert result["test_results"] == "`configured-tests` passed in 0s."
    assert "VALUE = 'human-approved'" in result["diff"]


def test_validation_trims_extra_blank_lines_at_end_of_reviewed_file(
    implementation_workspace,
    monkeypatch,
):
    run_id, _workspace, repo, source, config, task_data = implementation_workspace
    reviewed_code = "VALUE = 'human-approved'\n\n\n"
    saved_code = "VALUE = 'human-approved'\n"
    manual_implementation.apply_reviewed_files(
        run_id,
        config,
        task_data,
        {"src/app.py": reviewed_code},
    )
    monkeypatch.setattr(
        manual_implementation,
        "run_shell",
        lambda _command, cwd, _timeout: (
            0
            if cwd == repo and source.read_text(encoding="utf-8") == saved_code
            else 1,
            "configured checks passed",
        ),
    )

    result = manual_implementation.validate_and_commit(run_id, config, task_data)

    assert source.read_text(encoding="utf-8") == saved_code
    assert result["test_results"] == "`configured-tests` passed in 0s."


def test_validation_trims_reported_trailing_whitespace_from_reviewed_lines(
    implementation_workspace,
    monkeypatch,
):
    run_id, _workspace, repo, source, config, task_data = implementation_workspace
    reviewed_code = "VALUE = 'human-approved'\n    \n"
    normalized_code = "VALUE = 'human-approved'\n"
    manual_implementation.apply_reviewed_files(
        run_id,
        config,
        task_data,
        {"src/app.py": reviewed_code},
    )
    monkeypatch.setattr(
        manual_implementation,
        "run_shell",
        lambda _command, cwd, _timeout: (
            0 if cwd == repo else 1,
            "configured checks passed",
        ),
    )

    result = manual_implementation.validate_and_commit(run_id, config, task_data)

    assert source.read_text(encoding="utf-8") == normalized_code
    assert result["test_results"] == "`configured-tests` passed in 0s."


def test_reviewed_file_paths_cannot_escape_the_run_workspace(
    implementation_workspace,
):
    run_id, workspace, _repo, _source, config, task_data = implementation_workspace
    outside = workspace.parent / "outside.txt"
    task_data["proposed_files"] = [
        {"path": "../outside.txt", "content": "original", "deleted": False}
    ]

    with pytest.raises(ValueError, match="Invalid implementation path"):
        manual_implementation.apply_reviewed_files(
            run_id,
            config,
            task_data,
            {"../outside.txt": "changed"},
        )

    assert not outside.exists()


def test_validation_rejects_files_modified_by_the_test_command(
    implementation_workspace,
    monkeypatch,
):
    run_id, _workspace, repo, source, config, task_data = implementation_workspace
    human_code = "VALUE = 'human-approved'\n"
    manual_implementation.apply_reviewed_files(
        run_id,
        config,
        task_data,
        {"src/app.py": human_code},
    )

    def modify_during_tests(_command, cwd, _timeout):
        assert cwd == repo
        source.write_text("VALUE = 'changed-by-test'\n", encoding="utf-8")
        return 0, "checks passed"

    monkeypatch.setattr(manual_implementation, "run_shell", modify_during_tests)

    with pytest.raises(RuntimeError, match="modified repository files"):
        manual_implementation.validate_and_commit(run_id, config, task_data)

    assert _git(repo, "rev-parse", "HEAD") == task_data["base_sha"]
