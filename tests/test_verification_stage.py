import subprocess
from pathlib import Path
from types import SimpleNamespace

import openhands.sdk
import openhands.sdk.conversation
import pytest

from backend import jira_bot, runs_db


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("VERDICT: PASS\nNo blocking issues.", "PASS"),
        ("VERDICT: FAIL\nFix the assertion.", "FAIL"),
        ("The review is complete.\nVERDICT: PASS", None),
        ("**VERDICT: PASS**\nNo blocking issues.", None),
        ("", None),
    ],
)
def test_verification_verdict_requires_an_explicit_first_line(response, expected):
    assert jira_bot._verification_verdict(response) == expected


def test_verification_diff_includes_tracked_and_new_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "base"],
        cwd=repo,
        check=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    tracked.write_text("after\n", encoding="utf-8")
    (repo / "new.py").write_text("print('new')\n", encoding="utf-8")

    diff = jira_bot._collect_verification_diff(repo, base_commit)

    assert "tracked.txt" in diff
    assert "-before" in diff and "+after" in diff
    assert "new.py" in diff
    assert "+print('new')" in diff


def test_verification_diff_fails_closed_when_too_large(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
    )
    source = repo / "change.txt"
    source.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "change.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "base"],
        cwd=repo,
        check=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source.write_text("a diff too large for this test\n", encoding="utf-8")
    monkeypatch.setattr(jira_bot, "MAX_VERIFICATION_DIFF_CHARS", 10)

    with pytest.raises(RuntimeError, match="too large"):
        jira_bot._collect_verification_diff(repo, base_commit)


def test_extra_blank_line_at_eof_is_normalized_before_verification(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
    )
    source = repo / "app.py"
    source.write_bytes(b"print('ok')\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "base"],
        cwd=repo,
        check=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source.write_bytes(b"print('ok')\n\n")

    check = subprocess.run(
        ["git", "diff", "--check", base_commit],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "new blank line at EOF" in check.stdout

    normalized = jira_bot._normalize_reported_eof_blank_lines(
        repo,
        ["app.py"],
        check.stdout,
    )

    assert normalized == ["app.py"]
    assert source.read_bytes() == b"print('ok')\n"
    recheck = subprocess.run(
        ["git", "diff", "--check", base_commit],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert recheck.returncode == 0


def test_reported_whitespace_normalization_fixes_only_changed_lines(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
    )
    source = repo / "app.py"
    source.write_bytes(b"existing trailing space  \nprint('ok')\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "base"],
        cwd=repo,
        check=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source.write_bytes(b"existing trailing space  \nprint('ok')  \n\n")

    check = subprocess.run(
        ["git", "diff", "--check", base_commit],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "trailing whitespace" in check.stdout
    assert "new blank line at EOF" in check.stdout

    normalized = jira_bot._normalize_reported_eof_blank_lines(
        repo,
        ["app.py"],
        check.stdout,
    )

    assert normalized == ["app.py"]
    assert source.read_bytes() == b"existing trailing space  \nprint('ok')\n"
    recheck = subprocess.run(
        ["git", "diff", "--check", base_commit],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert recheck.returncode == 0


def test_verification_agent_has_no_repository_tools(monkeypatch, tmp_path):
    captured = {}

    class FakeLLM:
        def __init__(self, metrics=None):
            self.metrics = metrics if metrics is not None else []

        def model_copy(self, deep):
            assert deep is True
            return FakeLLM(self.metrics.copy())

        def reset_metrics(self):
            self.metrics = []

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["agent"] = kwargs

    class FakeConversation:
        def __init__(self, **kwargs):
            captured["conversation"] = kwargs
            self.state = SimpleNamespace(active_branch=lambda: ["agent event"])

        def send_message(self, message):
            captured["prompt"] = message

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(openhands.sdk, "Agent", FakeAgent)
    monkeypatch.setattr(openhands.sdk, "Conversation", FakeConversation)
    monkeypatch.setattr(
        openhands.sdk.conversation,
        "get_agent_final_response",
        lambda events: "VERDICT: PASS\nNo blocking defects.",
    )
    step_logs = []
    llm = FakeLLM(metrics=["coding-stage usage"])

    response = jira_bot._run_verification_agent(
        llm=llm,
        repo_dir=Path(tmp_path),
        prompt="Review this diff without editing.",
        on_step=lambda key, state, log=None: step_logs.append((key, state, log)),
    )

    assert response.startswith("VERDICT: PASS")
    assert captured["agent"]["tools"] == []
    assert captured["agent"]["llm"] is not llm
    assert captured["agent"]["llm"].metrics == []
    assert llm.metrics == ["coding-stage usage"]
    assert captured["conversation"]["workspace"] == str(tmp_path)
    assert captured["conversation"]["max_iteration_per_run"] == 12
    assert captured["prompt"] == "Review this diff without editing."
    assert captured["ran"] is True


def test_new_runs_expose_a_separate_verify_step(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_db, "DB_PATH", tmp_path / "runs.db")
    runs_db.init_db()
    runs_db.create_run("verification-run", "TEST-1", "2026-10-14T00:00:00+00:00")

    run = runs_db.get_run("verification-run")
    assert run is not None
    step_keys = [step["key"] for step in run["steps"]]
    assert step_keys == [
        "fetch",
        "assign",
        "gate",
        "in_progress",
        "agent",
        "verify",
        "pr",
        "jira",
    ]


def test_pull_request_creation_returns_the_github_url(monkeypatch):
    captured = {}

    def fake_gh_api(token, method, path, payload):
        captured.update(token=token, method=method, path=path, payload=payload)
        return {"html_url": "https://github.com/example/repo/pull/42"}

    monkeypatch.setattr(jira_bot, "gh_api", fake_gh_api)

    url = jira_bot._create_pull_request(
        "token",
        "example/repo",
        "A title",
        "feature-branch",
        "main",
        "PR body",
    )

    assert url == "https://github.com/example/repo/pull/42"
    assert captured == {
        "token": "token",
        "method": "POST",
        "path": "/repos/example/repo/pulls",
        "payload": {
            "title": "A title",
            "head": "feature-branch",
            "base": "main",
            "body": "PR body",
        },
    }


@pytest.mark.parametrize(
    "response",
    [
        [],
        {},
        {"html_url": ""},
        {"html_url": None},
    ],
)
def test_pull_request_creation_rejects_unusable_github_response(
    monkeypatch,
    response,
):
    monkeypatch.setattr(jira_bot, "gh_api", lambda *args: response)

    with pytest.raises(RuntimeError, match="pull request response|pull request URL"):
        jira_bot._create_pull_request(
            "token",
            "example/repo",
            "A title",
            "feature-branch",
            "main",
            "PR body",
        )


def _reviewer(monkeypatch, replies):
    """Fake read-only reviewer that answers each run() with the next reply."""
    sent = []
    queue = list(replies)

    class FakeLLM:
        metrics = []
        def model_copy(self, deep): return FakeLLM()
        def reset_metrics(self): pass

    current = {}

    class FakeConversation:
        def __init__(self, **kwargs):
            self.state = SimpleNamespace(active_branch=lambda: ["event"])
            current["conv"] = self
        def send_message(self, message): sent.append(message)
        def run(self): self.reply = queue.pop(0)

    monkeypatch.setattr(openhands.sdk, "Agent", lambda **kwargs: None)
    monkeypatch.setattr(openhands.sdk, "Conversation", FakeConversation)
    monkeypatch.setattr(openhands.sdk.conversation, "get_agent_final_response",
                        lambda events: current["conv"].reply)
    steps = []
    response = jira_bot._run_verification_agent(
        llm=FakeLLM(), repo_dir=Path("."), prompt="Review this diff.",
        on_step=lambda key, state, log=None: steps.append(log),
    )
    return response, sent, steps


def test_reviewer_that_forgets_its_verdict_is_asked_once_more(monkeypatch):
    response, sent, steps = _reviewer(monkeypatch, [
        "The endpoint is implemented correctly and nothing regresses.",
        "VERDICT: PASS\nThe endpoint is implemented correctly.",
    ])
    assert jira_bot._verification_verdict(response) == "PASS"
    assert sent == ["Review this diff.", jira_bot.VERDICT_REMINDER]
    assert any("asking it once more" in (s or "") for s in steps)


def test_reviewer_still_without_a_verdict_fails_closed(monkeypatch):
    response, sent, _ = _reviewer(monkeypatch, ["Looks fine to me.", "**VERDICT: PASS** looks fine"])
    assert jira_bot._verification_verdict(response) is None  # still needs a human
    assert len(sent) == 2  # asked only once more, never loops


def test_reviewer_with_a_verdict_is_not_asked_again(monkeypatch):
    _, sent, _ = _reviewer(monkeypatch, ["VERDICT: FAIL\nMissing tests."])
    assert sent == ["Review this diff."]
