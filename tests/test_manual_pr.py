import subprocess

import pytest

from backend import manual_pr
from backend.jira_bot import PipelineConfig


@pytest.fixture
def pr_workspace(tmp_path, monkeypatch):
    run_id = "674cb0b1-8c51-48ca-8d84-85fbbfa39000"
    repo = tmp_path / "repo"
    repo.mkdir()
    config = PipelineConfig(
        target_repo="owner/repo",
        target_branch="main",
        github_token="github-token-for-tests",
        app_name="Test app",
    )
    monkeypatch.setattr(
        manual_pr,
        "_repository_directory",
        lambda *_args: (tmp_path, repo),
    )
    monkeypatch.setattr(
        manual_pr,
        "_git_output",
        lambda command, _repo: {
            ("rev-parse", "--abbrev-ref", "HEAD"): "feature/674",
            ("rev-parse", "HEAD"): "a" * 40,
            ("status", "--porcelain"): "",
        }[tuple(command)],
    )
    return run_id, repo, config


def _provider_pr():
    return {
        "number": 42,
        "state": "open",
        "html_url": "https://github.com/owner/repo/pull/42",
        "head": {"ref": "feature/674"},
        "base": {"ref": "main"},
    }


def test_create_manual_pull_request_publishes_and_creates_once(
    pr_workspace,
    monkeypatch,
):
    run_id, _repo, config = pr_workspace
    pushes = []
    monkeypatch.setattr(
        manual_pr,
        "_git_push",
        lambda _config, _repo, branch, sha: pushes.append((branch, sha)),
    )
    calls = []

    def provider(_token, method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return []
        return _provider_pr()

    monkeypatch.setattr(manual_pr, "gh_api", provider)
    result = manual_pr.create_manual_pull_request(
        run_id,
        config,
        "674",
        "Support removing an attendee",
        "Allow registered attendees to unregister.",
        {
            "branch": "feature/674",
            "commit_sha": "a" * 40,
            "summary": "Added attendee removal.",
        },
        {"review_report": "VERDICT: PASS"},
    )

    assert pushes == [("feature/674", "a" * 40)]
    assert result == {
        "url": "https://github.com/owner/repo/pull/42",
        "number": 42,
        "source_branch": "feature/674",
        "target_branch": "main",
        "creation_status": "created",
        "state": "open",
        "evidence": (
            "Pull request #42 created; source feature/674 -> target main. "
            "GitHub confirmed it is open."
        ),
    }
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][2]["head"] == "feature/674"
    assert calls[1][2]["base"] == "main"


def test_retry_reuses_existing_open_pull_request(pr_workspace, monkeypatch):
    run_id, _repo, config = pr_workspace
    monkeypatch.setattr(manual_pr, "_git_push", lambda *_args: None)
    calls = []

    def provider(_token, method, path, _payload=None):
        calls.append((method, path))
        return (
            [_provider_pr()]
            if method == "GET"
            else pytest.fail("Retry must reuse the existing open PR")
        )

    monkeypatch.setattr(manual_pr, "gh_api", provider)
    result = manual_pr.create_manual_pull_request(
        run_id,
        config,
        "674",
        "Support removing an attendee",
        "Allow registered attendees to unregister.",
        {"branch": "feature/674", "commit_sha": "a" * 40},
        {},
    )

    assert result["creation_status"] == "existing"
    assert len(calls) == 1


def test_git_push_error_does_not_expose_token(pr_workspace, monkeypatch):
    _run_id, repo, config = pr_workspace
    secret = "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    config = PipelineConfig(
        target_repo=config.target_repo,
        target_branch=config.target_branch,
        github_token=secret,
        app_name=config.app_name,
    )
    monkeypatch.setattr(
        manual_pr.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"remote rejected https://{secret}@github.com/owner/repo.git",
        ),
    )

    with pytest.raises(RuntimeError) as error:
        manual_pr._git_push(config, repo, "feature/674", "a" * 40)

    assert secret not in str(error.value)
    assert "github.com" in str(error.value)
