import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import openhands.sdk
import openhands.sdk.conversation
from backend import implementation_evaluation
from backend.jira_bot import PipelineConfig
from openhands.sdk.event import MessageEvent
from openhands.sdk.llm import Message, TextContent


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


def _workspace(tmp_path: Path) -> tuple[Path, str, list[str]]:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    source = repo / "app.py"
    source.write_text("VALUE = 'base'\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "base")
    changed = ["app.py"]
    source.write_text("VALUE = 'proposal'\n", encoding="utf-8")
    return repo, _git(repo, "rev-parse", "HEAD"), changed


def _valid_assessment(*_args, **_kwargs):
    return {
        "requirements": [
            {"requirement": "Set a value", "status": "covered", "evidence": "app.py"},
            {
                "requirement": "Preserve behavior",
                "status": "partial",
                "evidence": "app.py",
            },
        ],
        "regression": {"findings": [], "summary": "No regression found."},
        "robustness": {
            "criteria": {
                "implementation_logic": {"status": "strong", "evidence": "app.py"},
                "edge_case_handling": {"status": "strong", "evidence": "app.py"},
                "integration_quality": {"status": "strong", "evidence": "app.py"},
            },
            "summary": "The implementation is clear and consistent.",
        },
        "static_analysis": {
            "status": "strong",
            "evidence": "The changed code follows the visible project patterns.",
            "summary": "No significant static quality concerns.",
        },
        "scope_safety": {
            "findings": [],
            "summary": "The implementation stays within the approved scope.",
        },
        "security": {
            "findings": [],
            "summary": "No concrete security issues found in the diff.",
        },
    }, []


def _reviewer_message(response: str) -> MessageEvent:
    return MessageEvent(
        source="agent",
        llm_message=Message(
            role="assistant",
            content=[TextContent(text=response)],
        ),
    )


@pytest.mark.parametrize("first_response", ["valid", "empty", "malformed"])
def test_independent_assessment_recovers_response_and_retries_invalid_output(
    monkeypatch,
    first_response,
):
    assessment_payload = _valid_assessment()[0]
    assessment_payload["robustness"]["criteria"] = [
        {"key": key, **value}
        for key, value in assessment_payload["robustness"]["criteria"].items()
    ]
    assessment_json = json.dumps(assessment_payload)

    class FakeConversation:
        def __init__(self, **_kwargs):
            self.events = []
            self.sent_messages = []
            self.state = SimpleNamespace(active_branch=lambda: self.events)
            self.run_count = 0

        def send_message(self, message):
            self.sent_messages.append(message)

        def run(self):
            self.run_count += 1
            if self.run_count == 1:
                if first_response == "valid":
                    self.events.append(_reviewer_message(assessment_json))
                elif first_response == "malformed":
                    self.events.append(_reviewer_message("not valid JSON"))
            else:
                self.events.append(_reviewer_message(assessment_json))

        def pause(self):
            pass

    class FakeLlm:
        def model_copy(self, **_kwargs):
            return self

        def reset_metrics(self):
            pass

    monkeypatch.setattr(openhands.sdk, "Agent", lambda **_kwargs: object())
    monkeypatch.setattr(openhands.sdk, "Conversation", FakeConversation)
    monkeypatch.setattr(
        openhands.sdk.conversation,
        "get_agent_final_response",
        lambda _events: "",
    )
    conversation_instances = []
    original_conversation = FakeConversation

    def create_conversation(**kwargs):
        conversation = original_conversation(**kwargs)
        conversation_instances.append(conversation)
        return conversation

    monkeypatch.setattr(openhands.sdk, "Conversation", create_conversation)

    assessment, errors = (
        implementation_evaluation._evaluate_requirements_and_regressions(
            llm=FakeLlm(),
            repo_dir=Path("."),
            prompt="Evaluate the implementation.",
            on_usage=None,
            timeout_sec=30,
            config=PipelineConfig(target_repo="owner/repo"),
        )
    )

    assert not errors
    assert assessment["requirements"][0]["requirement"] == "Set a value"
    conversation = conversation_instances[0]
    expected_runs = 1 if first_response == "valid" else 2
    assert conversation.run_count == expected_runs
    assert len(conversation.sent_messages) == expected_runs


def test_language_syntax_check_uses_javascript_checker(tmp_path, monkeypatch):
    source = tmp_path / "client.js"
    source.write_text("const answer = 42;\n", encoding="utf-8")
    commands = []

    def run_command(command, _worktree, _timeout=60):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(
        implementation_evaluation.shutil,
        "which",
        lambda name: f"C:\\tools\\{name}.exe" if name == "node" else None,
    )
    monkeypatch.setattr(implementation_evaluation, "_run_syntax_command", run_command)

    check = implementation_evaluation._evaluate_syntax_check(
        tmp_path,
        ["client.js"],
        PipelineConfig(target_repo="owner/repo"),
    )

    assert check["status"] == "passed"
    assert check["score"] == 15
    assert commands == [[r"C:\tools\node.exe", "--check", "client.js"]]


def test_language_syntax_check_uses_typescript_parser_for_tsx(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "component.tsx"
    source.write_text("export const View = () => <div />;\n", encoding="utf-8")
    parser_calls = []

    def run_typescript(worktree, source_files, dependency_root):
        parser_calls.append((worktree, source_files, dependency_root))
        return 0, ""

    monkeypatch.setattr(
        implementation_evaluation,
        "_typescript_syntax_check",
        run_typescript,
    )

    check = implementation_evaluation._evaluate_syntax_check(
        tmp_path,
        ["component.tsx"],
        PipelineConfig(target_repo="owner/repo"),
    )

    assert check["status"] == "passed"
    assert check["score"] == 15
    assert parser_calls == [(tmp_path, ["component.tsx"], None)]


def test_language_syntax_check_uses_java_parser(tmp_path, monkeypatch):
    source = tmp_path / "Sample.java"
    source.write_text("class Sample { }\n", encoding="utf-8")
    parser_calls = []

    def run_java(worktree, source_files):
        parser_calls.append((worktree, source_files))
        return 0, ""

    monkeypatch.setattr(implementation_evaluation, "_java_syntax_check", run_java)

    check = implementation_evaluation._evaluate_syntax_check(
        tmp_path,
        ["Sample.java"],
        PipelineConfig(target_repo="owner/repo"),
    )

    assert check["status"] == "passed"
    assert check["score"] == 15
    assert parser_calls == [(tmp_path, ["Sample.java"])]


def test_language_syntax_check_reports_unsupported_changed_source(tmp_path):
    source = tmp_path / "component.cs"
    source.write_text("class Component { }\n", encoding="utf-8")

    check = implementation_evaluation._evaluate_syntax_check(
        tmp_path,
        ["component.cs"],
        PipelineConfig(target_repo="owner/repo"),
    )

    assert check["status"] == "not_run"
    assert check["score"] == 0
    assert "no safe syntax checker configured" in check["output"]


def test_language_syntax_check_does_not_apply_to_documentation(tmp_path):
    (tmp_path / "README.md").write_text("# Change\n", encoding="utf-8")

    check = implementation_evaluation._evaluate_syntax_check(
        tmp_path,
        ["README.md"],
        PipelineConfig(target_repo="owner/repo"),
    )

    assert check["status"] == "passed"
    assert check["score"] == 15
    assert "No changed source files" in check["summary"]


def test_evaluation_uses_fixed_weights_and_reports_unavailable_checks(
    tmp_path,
    monkeypatch,
):
    repo, _base_sha, changed_files = _workspace(tmp_path)
    original = repo / "app.py"
    original_contents = original.read_text(encoding="utf-8")
    seen_worktree = []

    def python_checks(worktree, _python_files, _config):
        seen_worktree.append(worktree)
        assert (worktree / "app.py").read_text(
            encoding="utf-8"
        ) == "VALUE = 'proposal'\n"
        return [
            implementation_evaluation._check(
                "syntax", "Syntax", 15, "passed", "Compiled."
            ),
            implementation_evaluation._check(
                "static_analysis", "Static", 10, "passed", "Clean."
            ),
            implementation_evaluation._check(
                "security", "Security", 10, "passed", "Clean."
            ),
        ]

    monkeypatch.setattr(
        implementation_evaluation,
        "_evaluate_python_checks",
        python_checks,
    )
    monkeypatch.setattr(
        implementation_evaluation,
        "_evaluate_requirements_and_regressions",
        _valid_assessment,
    )

    evaluation = implementation_evaluation.evaluate_implementation(
        llm=object(),
        repo_dir=repo,
        config=PipelineConfig(target_repo="owner/repo"),
        ticket_key="T-1",
        summary="Set a value",
        description="Set a value and preserve behavior.",
        changed_files=changed_files,
        diff="diff",
    )

    assert evaluation["score"] == 94
    assert evaluation["scored_weight"] == 100
    robustness = next(
        item for item in evaluation["checks"] if item["key"] == "robustness"
    )
    assert robustness["score"] == 20
    assert original.read_text(encoding="utf-8") == original_contents
    assert seen_worktree and not seen_worktree[0].exists()


def test_robustness_findings_reduce_fixed_weight_score(
    tmp_path,
    monkeypatch,
):
    repo, _base_sha, changed_files = _workspace(tmp_path)

    def python_checks(_worktree, _python_files, _config):
        return [
            implementation_evaluation._check(
                "syntax", "Syntax", 15, "passed", "Compiled."
            ),
            implementation_evaluation._check(
                "static_analysis", "Static", 9, "failed", "One lint issue."
            ),
            implementation_evaluation._check(
                "security", "Security", 0, "not_run", "Scanner unavailable."
            ),
        ]

    def requirements_assessment(*_args, **_kwargs):
        return {
            "requirements": [
                {
                    "requirement": f"Requirement {index}",
                    "status": "covered",
                    "evidence": "app.py",
                }
                for index in range(7)
            ],
            "regression": {"findings": [], "summary": "No regression found."},
            "robustness": {
                "criteria": {
                    "implementation_logic": {
                        "status": "strong",
                        "evidence": "Correct result path.",
                    },
                    "edge_case_handling": {
                        "status": "partial",
                        "evidence": "Empty values are not handled.",
                    },
                    "integration_quality": {
                        "status": "weak",
                        "evidence": "The changed call violates the visible contract.",
                    },
                },
                "summary": "One area needs attention.",
            },
            "scope_safety": {
                "findings": [
                    {
                        "severity": "major",
                        "category": "scope",
                        "description": "Unrequested configuration changes.",
                        "evidence": "app.py: added unrelated setting",
                    }
                ],
                "summary": "The change includes substantial work outside the ticket.",
            },
            "static_analysis": _valid_assessment()[0]["static_analysis"],
            "security": _valid_assessment()[0]["security"],
        }, []

    monkeypatch.setattr(
        implementation_evaluation,
        "_evaluate_python_checks",
        python_checks,
    )
    monkeypatch.setattr(
        implementation_evaluation,
        "_evaluate_requirements_and_regressions",
        requirements_assessment,
    )

    evaluation = implementation_evaluation.evaluate_implementation(
        llm=object(),
        repo_dir=repo,
        config=PipelineConfig(target_repo="owner/repo"),
        ticket_key="T-674",
        summary="Implement behavior",
        description="Implement the approved behavior.",
        changed_files=changed_files,
        diff="diff",
    )

    assert evaluation["score"] == 75
    assert evaluation["scored_weight"] == 100
    robustness = next(
        item for item in evaluation["checks"] if item["key"] == "robustness"
    )
    assert robustness["score"] == 11
    assert robustness["status"] == "failed"
    scope_safety = next(
        item for item in evaluation["checks"] if item["key"] == "scope_safety"
    )
    assert scope_safety["score"] == 5
    assert scope_safety["status"] == "failed"


def test_failed_assessments_and_unrun_checks_do_not_normalize_to_high_score(
    tmp_path,
    monkeypatch,
):
    repo, _base_sha, changed_files = _workspace(tmp_path)

    def python_checks(_worktree, _python_files, _config):
        return [
            implementation_evaluation._check(
                "syntax", "Syntax", 15, "passed", "Compiled."
            ),
            implementation_evaluation._check(
                "static_analysis", "Static", 9, "warning", "One finding."
            ),
            implementation_evaluation._check(
                "security", "Security", 0, "not_run", "No scanner evidence."
            ),
        ]

    def assessment_failed(*_args, **_kwargs):
        return {}, [
            implementation_evaluation._check(
                "requirements",
                "Requirements",
                0,
                "error",
                "No coverage evidence.",
            ),
            implementation_evaluation._check(
                "regression",
                "Regression",
                0,
                "error",
                "No regression evidence.",
            ),
            implementation_evaluation._check(
                "robustness",
                "Robustness",
                0,
                "error",
                "No robustness evidence.",
            ),
            implementation_evaluation._check(
                "scope_safety",
                "Scope and safety",
                0,
                "error",
                "No scope/safety evidence.",
            ),
            implementation_evaluation._check(
                "static_analysis",
                "Static quality",
                0,
                "error",
                "No static quality evidence.",
            ),
            implementation_evaluation._check(
                "security",
                "Security",
                0,
                "error",
                "No security evidence.",
            ),
        ]

    monkeypatch.setattr(
        implementation_evaluation,
        "_evaluate_python_checks",
        python_checks,
    )
    monkeypatch.setattr(
        implementation_evaluation,
        "_evaluate_requirements_and_regressions",
        assessment_failed,
    )

    evaluation = implementation_evaluation.evaluate_implementation(
        llm=object(),
        repo_dir=repo,
        config=PipelineConfig(target_repo="owner/repo"),
        ticket_key="T-674",
        summary="Implement behavior",
        description="Implement the approved behavior.",
        changed_files=changed_files,
        diff="diff",
    )

    assert evaluation["score"] == 15
    assert evaluation["scored_weight"] == 15


def test_python_ast_checks_find_quality_and_security_patterns(tmp_path):
    source = tmp_path / "unsafe.py"
    source.write_text(
        "def collect(value=[]):\n"
        "    try:\n"
        "        return eval(input())\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )

    quality, security, error = implementation_evaluation._python_ast_findings(
        tmp_path,
        ["unsafe.py"],
    )

    assert error is None
    assert any("mutable function default" in item for item in quality)
    assert any("swallowed broad exception" in item for item in quality)
    assert any("dynamic eval execution" in item for item, _, _ in security)
    assert any(critical for _, _, critical in security)


def test_python_ast_checks_report_unparseable_source(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    quality, security, error = implementation_evaluation._python_ast_findings(
        tmp_path,
        ["broken.py"],
    )

    assert quality == []
    assert security == []
    assert error is not None
    assert "broken.py" in error
