import subprocess
from pathlib import Path

import pytest

from backend import manual_verification
from backend.jira_bot import PYTEST_NO_TESTS_EXIT, PipelineConfig


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
def verification_workspace(tmp_path, monkeypatch):
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
    _git(repo, "checkout", "-b", "feat/674-verification")
    source.write_text("VALUE = 'implemented'\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "implementation")
    commit_sha = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        manual_verification,
        "manual_workspace_path",
        lambda _run_id: workspace,
    )
    monkeypatch.setattr(
        manual_verification.skills_store,
        "select_persona",
        lambda *_args, **_kwargs: "test-reviewer",
    )
    monkeypatch.setattr(
        manual_verification.skills_store,
        "render_skill",
        lambda *_args, **_kwargs: "Review requirements carefully.",
    )
    monkeypatch.setattr(
        manual_verification.skills_store,
        "detect_modifiers",
        lambda _repo: [],
    )
    monkeypatch.setattr(
        manual_verification,
        "_run_verification_agent",
        lambda _llm, _repo, _prompt, _event, _usage: (
            "VERDICT: PASS\nSUMMARY\nRequirements are satisfied.\n"
            "REQUIREMENT COVERAGE\nAll requirements are covered.\n"
            "FINDINGS\nNone."
        ),
    )
    config = PipelineConfig(
        target_repo="owner/repo",
        target_branch="main",
        azure_key="test-key",
        verify_mode="command",
        test_command="pytest -q",
        test_timeout_min=1,
    )
    requirements = {
        "ticket_key": "674",
        "summary": "Add unregister endpoint",
        "description": "Remove a registered user from an activity.",
    }
    implementation = {
        "source": "manual_agent",
        "summary": "Added the unregister endpoint.",
        "branch": "feat/674-verification",
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "changed_files": "src/app.py",
        "test_results": "Implementation-stage checks passed.",
    }
    return run_id, workspace, repo, requirements, implementation, config


@pytest.mark.parametrize(
    ("test_return_code", "expected_decision", "expected_status"),
    [
        (0, "pass", "passed"),
        (1, "fail", "failed"),
        (PYTEST_NO_TESTS_EXIT, "pass", "not_run"),
    ],
)
def test_manual_verification_runs_checks_in_isolated_worktree(
    verification_workspace,
    monkeypatch,
    test_return_code,
    expected_decision,
    expected_status,
):
    run_id, workspace, repo, requirements, implementation, config = (
        verification_workspace
    )
    check_directories = []

    def run_checks(_command, cwd, _timeout):
        check_directories.append(cwd)
        assert cwd != repo
        assert cwd.is_dir()
        return test_return_code, "test execution output"

    monkeypatch.setattr(manual_verification, "run_shell", run_checks)

    report = manual_verification.verify_implementation(
        run_id,
        config,
        requirements,
        implementation,
    )

    assert report["decision"] == expected_decision
    assert report["checks"][0]["status"] == "passed"
    assert report["checks"][1]["status"] == expected_status
    assert check_directories
    assert all(not path.exists() for path in check_directories)
    assert _git(repo, "rev-parse", "HEAD") == implementation["commit_sha"]
    assert _git(repo, "status", "--porcelain") == ""
    assert (workspace / "repo").is_dir()


def test_no_test_collection_is_a_limitation_not_a_blocking_failure(
    verification_workspace,
    monkeypatch,
):
    run_id, _workspace, _repo, requirements, implementation, config = (
        verification_workspace
    )
    reviewer_prompts = []
    monkeypatch.setattr(
        manual_verification,
        "run_shell",
        lambda *_args: (PYTEST_NO_TESTS_EXIT, "no tests collected"),
    )

    def review(_llm, _repo, prompt, _event, _usage):
        reviewer_prompts.append(prompt)
        return (
            "VERDICT: PASS\nSUMMARY\nThe implementation meets the requirements.\n"
            "REQUIREMENT COVERAGE\nAll requirements are covered by the diff.\n"
            "FINDINGS\nNo blocking defects. No tests were collected."
        )

    monkeypatch.setattr(manual_verification, "_run_verification_agent", review)

    report = manual_verification.verify_implementation(
        run_id,
        config,
        requirements,
        implementation,
    )

    assert report["decision"] == "pass"
    assert report["checks"][1]["status"] == "not_run"
    assert "not blocking defects by themselves" in reviewer_prompts[0]


def test_manual_verification_fails_closed_on_incomplete_reviewer_report(
    verification_workspace,
    monkeypatch,
):
    run_id, _workspace, _repo, requirements, implementation, config = (
        verification_workspace
    )
    monkeypatch.setattr(
        manual_verification,
        "run_shell",
        lambda *_args: (0, "all tests passed"),
    )
    monkeypatch.setattr(
        manual_verification,
        "_run_verification_agent",
        lambda *_args: "VERDICT: PASS\nLooks good.",
    )

    report = manual_verification.verify_implementation(
        run_id,
        config,
        requirements,
        implementation,
    )

    assert report["decision"] == "fail"
    assert report["reviewer_verdict"] == "INCOMPLETE"
    assert "required report format" in report["decision_reason"]
    assert report["checks"][-1]["name"] == "Reviewer report"


def test_manual_verification_accepts_substantive_verdict_without_headings(
    verification_workspace,
    monkeypatch,
):
    run_id, _workspace, _repo, requirements, implementation, config = (
        verification_workspace
    )
    monkeypatch.setattr(
        manual_verification,
        "run_shell",
        lambda *_args: (0, "all tests passed"),
    )
    monkeypatch.setattr(
        manual_verification,
        "_run_verification_agent",
        lambda *_args: (
            "VERDICT: PASS\n"
            "The diff satisfies the requested behavior and has no blocking defects."
        ),
    )

    report = manual_verification.verify_implementation(
        run_id,
        config,
        requirements,
        implementation,
    )

    assert report["decision"] == "pass"
    assert report["reviewer_verdict"] == "PASS"
    assert report["checks"][-1]["status"] == "warning"
    assert "omitted required section(s)" in report["checks"][-1]["summary"]


def test_manual_verification_preserves_an_explicit_reviewer_failure(
    verification_workspace,
    monkeypatch,
):
    run_id, _workspace, _repo, requirements, implementation, config = (
        verification_workspace
    )
    monkeypatch.setattr(
        manual_verification,
        "run_shell",
        lambda *_args: (0, "all tests passed"),
    )
    monkeypatch.setattr(
        manual_verification,
        "_run_verification_agent",
        lambda *_args: (
            "VERDICT: FAIL\nSUMMARY\nMissing behavior.\n"
            "REQUIREMENT COVERAGE\nOne requirement is not met.\n"
            "FINDINGS\nsrc/app.py does not handle the empty input case."
        ),
    )

    report = manual_verification.verify_implementation(
        run_id,
        config,
        requirements,
        implementation,
    )

    assert report["decision"] == "fail"
    assert report["reviewer_verdict"] == "FAIL"
    assert "Reviewer returned FAIL" in report["decision_reason"]
