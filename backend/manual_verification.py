"""Independent verification of implementation artifacts in Manual workflows."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import SecretStr

from backend import skills_store
from backend.jira_bot import (
    PYTEST_NO_TESTS_EXIT,
    PipelineConfig,
    _run_verification_agent,
    _tail,
    redact,
    run_shell,
)
from backend.manual_implementation import manual_workspace_path


def _git(command: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run git {' '.join(command)}: {exc}") from exc
    if result.returncode:
        raise RuntimeError(
            f"Git {' '.join(command)} failed: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    return result.stdout.strip()


def _check(name: str, status: str, summary: str, output: str = "") -> dict[str, str]:
    return {"name": name, "status": status, "summary": summary, "output": output}


def verify_implementation(
    run_id: str,
    config: PipelineConfig,
    requirements: dict[str, str],
    implementation: dict,
    on_event: Callable[[str, str, str | None], None] | None = None,
    on_usage: Callable[[str, int, int, float | None, int, int], None] | None = None,
) -> dict:
    """Run configured checks and a read-only requirements review against a commit."""
    if not implementation.get("source") == "manual_agent":
        raise RuntimeError(
            "Manual verification requires saved agent implementation artifacts"
        )
    summary = requirements.get("summary", "").strip()
    description = requirements.get("description", "").strip()
    branch = implementation.get("branch", "")
    base_sha = implementation.get("base_sha", "")
    commit_sha = implementation.get("commit_sha", "")
    if not all((summary, description, branch, base_sha, commit_sha)):
        raise RuntimeError(
            "Approved requirements or implementation artifacts are missing"
        )

    workspace = manual_workspace_path(run_id)
    repo_dir = workspace / config.target_repo.rsplit("/", 1)[-1]
    try:
        repo_dir.resolve(strict=True).relative_to(workspace.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RuntimeError("The implementation workspace is unavailable") from exc
    if not repo_dir.is_dir():
        raise RuntimeError("The implementation workspace is unavailable")
    if _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir) != branch:
        raise RuntimeError("The implementation workspace is not on the saved branch")
    if _git(["rev-parse", "HEAD"], repo_dir) != commit_sha:
        raise RuntimeError("The implementation commit changed after human approval")
    if _git(["status", "--porcelain"], repo_dir):
        raise RuntimeError(
            "The approved implementation workspace contains unsaved changes"
        )

    diff = _git(
        ["diff", "--no-ext-diff", "--unified=3", base_sha, commit_sha],
        repo_dir,
    )
    if not diff:
        raise RuntimeError("The approved implementation diff is empty")
    if len(diff) > 120_000:
        raise RuntimeError(
            "The implementation diff exceeds the verification review limit"
        )

    verify_workspace = workspace / f"verification-{uuid.uuid4().hex}"
    checks: list[dict[str, str]] = []
    try:
        _git(
            ["worktree", "add", "--detach", str(verify_workspace), commit_sha], repo_dir
        )
        try:
            diff_check = subprocess.run(
                ["git", "diff", "--check", base_sha, commit_sha],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(
                _check(
                    "git diff --check",
                    "error",
                    "Could not run the whitespace and conflict-marker check.",
                    str(exc)[:2000],
                )
            )
        else:
            if diff_check.returncode == 0:
                checks.append(
                    _check("git diff --check", "passed", "No whitespace errors found.")
                )
            else:
                checks.append(
                    _check(
                        "git diff --check",
                        "failed",
                        "Whitespace or conflict-marker errors were found.",
                        (diff_check.stdout or diff_check.stderr)[:6000],
                    )
                )

        if config.verify_mode == "command":
            if config.test_setup_command:
                if on_event:
                    on_event(
                        "in-progress", "Running configured verification setup", None
                    )
                setup_rc, setup_output = run_shell(
                    config.test_setup_command,
                    verify_workspace,
                    config.test_timeout_min * 60,
                )
                if setup_rc == 0:
                    checks.append(
                        _check("Setup", "passed", "Configured test setup passed.")
                    )
                else:
                    why = (
                        "timed out"
                        if setup_rc is None
                        else f"exited with code {setup_rc}"
                    )
                    checks.append(
                        _check(
                            "Setup",
                            "failed",
                            f"Configured test setup {why}.",
                            _tail(setup_output, 30)[:6000],
                        )
                    )
            if config.test_command and (
                not config.test_setup_command or checks[-1]["status"] == "passed"
            ):
                if on_event:
                    on_event(
                        "in-progress", "Running configured verification tests", None
                    )
                started = time.monotonic()
                test_rc, test_output = run_shell(
                    config.test_command,
                    verify_workspace,
                    config.test_timeout_min * 60,
                )
                elapsed = int(time.monotonic() - started)
                if test_rc == 0:
                    checks.append(
                        _check(
                            "Tests",
                            "passed",
                            f"`{config.test_command}` passed in {elapsed}s.",
                            _tail(test_output, 30)[:6000],
                        )
                    )
                elif (
                    test_rc == PYTEST_NO_TESTS_EXIT and "pytest" in config.test_command
                ):
                    checks.append(
                        _check(
                            "Tests",
                            "not_run",
                            f"No tests were collected by `{config.test_command}`.",
                            _tail(test_output, 30)[:6000],
                        )
                    )
                else:
                    why = (
                        "timed out"
                        if test_rc is None
                        else f"exited with code {test_rc}"
                    )
                    checks.append(
                        _check(
                            "Tests",
                            "failed",
                            f"Configured tests {why}.",
                            _tail(test_output, 30)[:6000],
                        )
                    )
            elif not config.test_command:
                checks.append(
                    _check("Tests", "not_run", "No test command is configured.")
                )
            elif config.test_setup_command and checks[-1]["status"] != "passed":
                checks.append(
                    _check(
                        "Tests", "not_run", "Tests were skipped because setup failed."
                    )
                )
        elif config.verify_mode == "github_actions":
            checks.append(
                _check(
                    "CI",
                    "not_run",
                    "GitHub Actions runs after the pull request is opened.",
                )
            )
        else:
            checks.append(
                _check(
                    "Configured tests",
                    "not_run",
                    "No automated test command is configured.",
                )
            )
    finally:
        if verify_workspace.exists():
            try:
                _git(["worktree", "remove", "--force", str(verify_workspace)], repo_dir)
            except RuntimeError:
                shutil.rmtree(verify_workspace, ignore_errors=True)

    persona = skills_store.select_persona(
        summary,
        description,
        stage="verify",
        app_id=config.app_id,
    )
    variables = {
        "TICKET_KEY": requirements.get("ticket_key", ""),
        "TICKET_SUMMARY": summary,
        "TARGET_REPO": config.target_repo,
        "TARGET_BRANCH": config.target_branch,
        "PERSONA": persona,
    }
    persona_content = skills_store.render_skill(
        persona,
        stage="verify",
        app_id=config.app_id,
        **variables,
    )
    modifiers = skills_store.detect_modifiers(repo_dir)
    modifier_block = (
        f"--- STACK NOTES: {', '.join(modifiers)} ---\n"
        f"{skills_store.render_modifiers(modifiers, **variables)}\n"
        "--- END STACK NOTES ---\n\n"
        if modifiers
        else ""
    )
    test_summary = "\n".join(
        f"- {item['name']}: {item['status']} — {item['summary']}" for item in checks
    )
    implementation_test_output = redact(
        implementation.get("test_output", "")[:6000],
        config.github_token,
        config.azure_key,
        config.ado_pat,
        config.jira_api_token,
    )
    prompt = (
        f"--- VERIFICATION PERSONA: {persona} ---\n{persona_content}\n"
        "--- END VERIFICATION PERSONA ---\n\n"
        f"{modifier_block}"
        "Independently verify ticket "
        f"{requirements.get('ticket_key', '')}: {summary}\n\n"
        f"Approved requirements:\n{description}\n\n"
        f"Branch: {branch}\n"
        f"Implementation summary: {implementation.get('summary', '')}\n"
        f"Changed files: {implementation.get('changed_files', '')}\n"
        f"Previous implementation checks: {implementation.get('test_results', '')}\n"
        f"Previous implementation test output:\n"
        f"{implementation_test_output}\n"
        f"Independent verification checks:\n{test_summary}\n\n"
        "Review the complete diff against the approved requirements. Identify "
        "requirement coverage, concrete defects, regressions, and test coverage "
        "limitations. Missing tests or a check explicitly marked not_run are "
        "limitations to report, not blocking defects by themselves; do not FAIL "
        "solely because no tests exist or no test command is configured. Assess "
        "whether the code visibly satisfies the requirements and whether any "
        "concrete blocking defect is present. Do not edit or execute repository "
        "code, and do not claim checks were run unless listed above. PASS when "
        "the diff satisfies the requirements and no blocking issue is apparent; "
        "FAIL only for a concrete blocking defect or when a requirement cannot "
        "be assessed from the available implementation evidence. The first "
        "non-empty response line must be exactly `VERDICT: PASS` or "
        "`VERDICT: FAIL`. Then give a concise rationale covering requirement "
        "coverage and findings; headings are preferred but not required. "
        "Include file paths for any concrete findings.\n\n"
        f"--- DIFF START ---\n{diff}\n--- DIFF END ---"
    )
    from openhands.sdk import LLM

    llm = LLM(
        model=config.verification_model,
        api_key=SecretStr(config.azure_key),
        base_url=config.azure_url,
    )
    response = _run_verification_agent(
        llm,
        repo_dir,
        prompt,
        lambda _stage, state, message: on_event(state, message, None)
        if on_event and message
        else None,
        on_usage,
    ).strip()
    first_line = next(
        (line.strip() for line in response.splitlines() if line.strip()), ""
    )
    verdict_match = re.fullmatch(r"VERDICT:\s*(PASS|FAIL)", first_line)
    missing_sections = [
        section
        for section in ("SUMMARY", "REQUIREMENT COVERAGE", "FINDINGS")
        if not re.search(
            rf"(?im)^\s*(?:#+\s*)?\*{{0,2}}{re.escape(section)}\*{{0,2}}"
            r"(?=\s*:|\s*$)",
            response,
        )
    ]
    reviewer_rationale = "\n".join(response.splitlines()[1:]).strip()
    reviewer_report_complete = (
        verdict_match is not None and len(reviewer_rationale) >= 20
    )
    if missing_sections:
        checks.append(
            _check(
                "Reviewer report",
                "warning",
                "The reviewer report omitted required section(s): "
                + ", ".join(missing_sections)
                + ". Its explicit verdict and rationale were still evaluated.",
            )
        )
    elif not reviewer_report_complete:
        checks.append(
            _check(
                "Reviewer report",
                "failed",
                "The reviewer response must include an explicit verdict and "
                "substantive rationale.",
                response[:6000],
            )
        )
    reviewer_passed = (
        reviewer_report_complete
        and verdict_match.group(1) == "PASS"
    )
    required_checks_passed = all(
        item["status"] in ("passed", "warning")
        or (
            config.verify_mode == "github_actions"
            and item["name"] == "CI"
            and item["status"] == "not_run"
        )
        or (
            item["name"] in ("Tests", "Configured tests")
            and item["status"] == "not_run"
        )
        for item in checks
    )
    decision = "pass" if reviewer_passed and required_checks_passed else "fail"
    if not reviewer_report_complete:
        reviewer_verdict = "INCOMPLETE"
    else:
        reviewer_verdict = verdict_match.group(1)
    failure_reasons = []
    if reviewer_verdict != "PASS":
        failure_reasons.append(
            "Reviewer returned FAIL."
            if reviewer_verdict == "FAIL"
            else "Reviewer response did not satisfy the required report format."
        )
    failed_checks = [
        f"{item['name']}: {item['summary']}"
        for item in checks
        if item["status"] not in ("passed",)
        and item["status"] != "warning"
        and not (
            config.verify_mode == "github_actions"
            and item["name"] == "CI"
            and item["status"] == "not_run"
        )
        and not (
            item["name"] in ("Tests", "Configured tests")
            and item["status"] == "not_run"
        )
    ]
    failure_reasons.extend(failed_checks)
    return {
        "decision": decision,
        "reviewer_verdict": reviewer_verdict,
        "decision_reason": (
            " ".join(failure_reasons)
            if failure_reasons
            else "Independent checks and reviewer passed."
        ),
        "summary": implementation.get("summary", ""),
        "branch": branch,
        "changed_files": implementation.get("changed_files", ""),
        "test_results": implementation.get("test_results", ""),
        "implementation_test_output": implementation_test_output,
        "checks": checks,
        "review_report": redact(
            response[:12_000],
            config.github_token,
            config.azure_key,
            config.ado_pat,
            config.jira_api_token,
        ),
    }
