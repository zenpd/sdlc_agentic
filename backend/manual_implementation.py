"""Workspace operations for the human-reviewed Manual implementation stage."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable

from pydantic import SecretStr

from backend import skills_store
from backend.jira_bot import (
    AGENT_MAX_STEPS,
    PYTEST_NO_TESTS_EXIT,
    PipelineConfig,
    _make_agent_event_logger,
    _normalize_reported_eof_blank_lines,
    _report_usage,
    _tail,
    git_failure_message,
    protected_path_violations,
    redact,
    run_cmd,
    run_shell,
)


MAX_MANUAL_PATCH_BYTES = 600_000
MAX_MANUAL_FILE_BYTES = 300_000
IMPLEMENTATION_SCORE_THRESHOLD = 85


def manual_workspace_path(run_id: str) -> Path:
    """Resolve a run's workspace without allowing caller-controlled paths."""
    try:
        safe_run_id = str(uuid.UUID(run_id))
    except ValueError as exc:
        raise ValueError("Invalid run identifier for implementation workspace") from exc
    return Path(__file__).parent / "manual_workspaces" / safe_run_id


def _repository_directory(run_id: str, config: PipelineConfig) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config.target_repo) or any(
        part in (".", "..") for part in config.target_repo.split("/")
    ):
        raise ValueError(
            "Configured target repository must use owner/repository format"
        )
    workspace = manual_workspace_path(run_id)
    repo_dir = workspace / config.target_repo.rsplit("/", 1)[-1]
    try:
        repo_dir.resolve(strict=False).relative_to(workspace.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(
            "Configured repository path escapes the run workspace"
        ) from exc
    return workspace, repo_dir


def _run_checked(command: list[str], cwd: Path, action: str) -> str:
    rc, stdout, stderr = run_cmd(command, cwd, silent=True)
    if rc != 0:
        raise RuntimeError(f"{action} failed: {(stderr or stdout).strip()[:500]}")
    return stdout.strip()


def _changed_files(repo_dir: Path, base_sha: str) -> list[str]:
    _run_checked(["git", "add", "-N", "-A"], repo_dir, "Preparing proposed changes")
    paths = _run_checked(
        ["git", "diff", "--name-only", base_sha], repo_dir, "Listing proposed changes"
    ).splitlines()
    return list(dict.fromkeys(path.strip() for path in paths if path.strip()))


def _trim_extra_blank_lines_at_eof(content: str) -> str:
    trimmed = content.rstrip("\r\n")
    trailing_newlines = content[len(trimmed) :]
    if trailing_newlines.count("\n") <= 1:
        return content
    line_ending = "\r\n" if trailing_newlines.endswith("\r\n") else "\n"
    return trimmed + line_ending


def _read_proposed_files(repo_dir: Path, changed_files: list[str]) -> list[dict]:
    files = []
    total_bytes = 0
    for relative_path in changed_files:
        path = repo_dir / relative_path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(repo_dir.resolve(strict=True))
        except (OSError, ValueError) as exc:
            if not path.exists():
                files.append({"path": relative_path, "content": "", "deleted": True})
                continue
            raise RuntimeError(
                f"Changed path is outside the implementation workspace: {relative_path}"
            ) from exc
        if path.is_symlink() or not resolved.is_file():
            raise RuntimeError(
                "Manual review currently supports regular text files only: "
                f"{relative_path}"
            )
        contents = resolved.read_bytes()
        total_bytes += len(contents)
        if b"\0" in contents:
            raise RuntimeError(
                "Manual review currently supports regular text files only: "
                f"{relative_path}"
            )
        if total_bytes > MAX_MANUAL_FILE_BYTES:
            raise RuntimeError(
                "The proposed source files exceed the manual editor size limit"
            )
        try:
            text = contents.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"Manual review requires UTF-8 text files: {relative_path}"
            ) from exc
        files.append({"path": relative_path, "content": text, "deleted": False})
    return files


def _proposed_patch(repo_dir: Path, base_sha: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", base_sha],
            cwd=repo_dir,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not collect proposed patch: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            "Could not collect proposed patch: "
            + result.stderr.decode(errors="replace")[:500]
        )
    patch = result.stdout.decode("utf-8", errors="replace")
    if not patch.strip():
        raise RuntimeError("The coding agent did not produce any repository changes")
    if len(result.stdout) > MAX_MANUAL_PATCH_BYTES:
        raise RuntimeError("The proposed patch exceeds the manual review size limit")
    return patch


def review_snapshot(repo_dir: Path, base_sha: str) -> dict:
    changed_files = _changed_files(repo_dir, base_sha)
    if not changed_files:
        raise RuntimeError("The reviewed implementation contains no changes")
    return {
        "changed_files": changed_files,
        "proposed_files": _read_proposed_files(repo_dir, changed_files),
        "proposed_patch": _proposed_patch(repo_dir, base_sha),
    }


def _run_agent_for_review(
    conversation,
    task_prompt: str,
    repo_dir: Path,
    base_sha: str,
    get_final_response: Callable,
    is_timed_out: Callable[[], bool],
) -> tuple[str, dict]:
    for attempt in range(2):
        message = task_prompt
        if attempt:
            message = (
                "Your previous attempt completed without changing any repository "
                "files. Re-read the approved ticket and relevant source/tests, then "
                "implement the requested behavior in the workspace. Do not finish "
                "with only a summary: inspect the final git diff and make sure the "
                "requested code or test changes are actually present. If the ticket "
                "is already fully implemented, explain why no code change is "
                "necessary instead of making an unrelated edit.\n\n"
                + task_prompt
            )
        conversation.send_message(message)
        conversation.run()
        state = getattr(
            getattr(getattr(conversation, "state", None), "execution_status", None),
            "value",
            "",
        )
        if is_timed_out() or state in ("error", "stuck"):
            raise RuntimeError(
                "The coding agent reached its time or step limit before completing"
            )
        agent_summary = get_final_response(
            conversation.state.active_branch()
        ).strip()
        try:
            return agent_summary, review_snapshot(repo_dir, base_sha)
        except RuntimeError as exc:
            if (
                attempt == 0
                and str(exc) == "The reviewed implementation contains no changes"
            ):
                continue
            if str(exc) == "The reviewed implementation contains no changes":
                raise RuntimeError(
                    "The coding agent completed twice without changing any "
                    "repository files. Add specific retry instructions or confirm "
                    "that the approved ticket requires a code change."
                ) from exc
            raise
    raise RuntimeError("The coding agent could not prepare an implementation proposal")


def generate_implementation(
    run: dict,
    config: PipelineConfig,
    results: dict[str, dict],
    task_data: dict,
    feedback: str = "",
    on_agent_event: Callable | None = None,
    on_usage: Callable | None = None,
    on_checkpoint: Callable | None = None,
) -> dict:
    """Clone or resume the run workspace and return a human-reviewable proposal."""
    workspace, repo_dir = _repository_directory(run["id"], config)
    secrets = (config.github_token, config.azure_key)
    if repo_dir.exists() and not (
        task_data.get("branch") and task_data.get("base_sha")
    ):
        shutil.rmtree(repo_dir)
    if not repo_dir.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        repo_url = f"https://{config.github_token}@github.com/{config.target_repo}.git"
        rc, _out, err = run_cmd(
            ["git", "clone", "--branch", config.target_branch, repo_url, str(repo_dir)],
            cwd=workspace,
            silent=True,
        )
        if rc != 0:
            raise RuntimeError(
                git_failure_message(
                    f"Cloning {config.target_repo}",
                    err,
                    config.app_name,
                    *secrets,
                )
            )
        _run_checked(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                f"https://github.com/{config.target_repo}.git",
            ],
            repo_dir,
            "Removing credentials from repository configuration",
        )
        branch = task_data.get("branch")
        if not branch:
            ticket = re.sub(r"[^a-zA-Z0-9-]+", "-", run["ticket_key"]).strip("-")
            summary = (
                results.get("gate", {})
                .get("result", {})
                .get(
                    "summary",
                    results.get("fetch", {}).get("result", {}).get("summary", ""),
                )
            )
            slug = (
                re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:35].strip("-")
            )
            branch = f"{config.branch_prefix}{ticket}-{slug}-{run['id'][:6]}"
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or ".." in branch.split(
            "/"
        ):
            raise RuntimeError("The generated implementation branch name is invalid")
        _run_checked(
            ["git", "checkout", "-b", branch],
            repo_dir,
            "Creating implementation branch",
        )
        base_sha = _run_checked(
            ["git", "rev-parse", "HEAD"], repo_dir, "Reading target branch"
        )
        task_data.update({"branch": branch, "base_sha": base_sha})
        if on_checkpoint is not None:
            on_checkpoint(task_data)
    else:
        branch = task_data.get("branch", "")
        base_sha = task_data.get("base_sha", "")
        if not branch or not base_sha:
            raise RuntimeError(
                "The saved implementation workspace is missing its branch checkpoint"
            )
        _run_checked(
            ["git", "checkout", branch], repo_dir, "Resuming implementation branch"
        )

    if (
        config.verify_mode == "command"
        and config.test_setup_command
        and not task_data.get("setup_complete")
    ):
        rc, output = run_shell(
            config.test_setup_command,
            repo_dir,
            config.test_timeout_min * 60,
        )
        if rc != 0:
            why = "timed out" if rc is None else f"exited with code {rc}"
            raise RuntimeError(
                f"Configured test setup {why}: {config.test_setup_command}\n"
                f"{_tail(output)}"
            )
        task_data["setup_complete"] = True
        if on_checkpoint is not None:
            on_checkpoint(task_data)

    ticket_result = results.get("gate", {}).get("result", {})
    fetched = results.get("fetch", {}).get("result", {})
    summary = ticket_result.get("summary") or fetched.get("summary", "")
    description = ticket_result.get("description") or fetched.get("description", "")
    if not summary or not description:
        raise RuntimeError("Approved ticket summary and description are missing")

    pipeline_stage = "code"
    eligible_personas = {
        skill["slug"]
        for skill in skills_store.list_skills(
            stage=pipeline_stage, app_id=config.app_id
        )
        if "error" not in skill
    }
    persona = (
        config.default_persona
        if config.default_persona in eligible_personas
        else skills_store.select_persona(
            summary,
            description,
            stage=pipeline_stage,
            app_id=config.app_id,
        )
    )
    skill_vars = {
        "TICKET_KEY": run["ticket_key"],
        "TICKET_SUMMARY": summary,
        "TARGET_REPO": config.target_repo,
        "TARGET_BRANCH": config.target_branch,
        "PERSONA": persona,
    }
    persona_content = skills_store.render_skill(
        persona,
        stage=pipeline_stage,
        app_id=config.app_id,
        **skill_vars,
    )
    modifier_block = ""
    modifiers = skills_store.detect_modifiers(repo_dir)
    if modifiers:
        modifier_block = (
            f"--- STACK NOTES: {', '.join(modifiers)} ---\n"
            f"{skills_store.render_modifiers(modifiers, **skill_vars)}\n"
            "--- END STACK NOTES ---\n\n"
        )

    task_prompt = (
        f"--- PERSONA: {persona} ---\n{persona_content}\n--- END PERSONA ---\n\n"
        f"{modifier_block}"
        f"Approved ticket {run['ticket_key']}: {summary}\n\n"
        f"Approved description:\n{description}\n\n"
        f"Repository: {config.target_repo}\n"
        f"Target branch: {config.target_branch}\n"
        f"Implementation branch: {branch}\n"
        f"Workspace: {repo_dir}\n\n"
        "Before coding, inspect and follow repository instructions, including "
        "AGENTS.md files and repository-level coding instructions such as "
        ".github/copilot-instructions.md. Load relevant source files and tests "
        "to understand the implementation context. Implement the approved "
        "requirements in the relevant files. Do not ask the user questions; "
        "the clarity gate has already approved this ticket. Do not commit, push, "
        "or change the configured target branch. Do not install packages or start "
        "a server. Do not modify protected paths. Finish with a concise summary "
        "of the implementation. Do not claim tests or checks passed unless you "
        "actually ran them and observed their results."
    )
    if feedback:
        task_prompt += f"\n\nHuman review feedback to address:\n{feedback.strip()}"
    if config.protected_paths:
        task_prompt += (
            "\n\nProtected paths that must not be created, edited, or deleted: "
            + ", ".join(config.protected_paths)
        )

    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.sdk.conversation import get_agent_final_response
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool
    from openhands.tools.terminal import TerminalTool

    llm = LLM(
        model=config.coding_model,
        api_key=SecretStr(config.azure_key),
        base_url=config.azure_url,
    )
    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
    )
    conversation = Conversation(
        agent=agent,
        workspace=str(repo_dir),
        callbacks=[_make_agent_event_logger(on_agent_event or (lambda *_args: None))],
        max_iteration_per_run=AGENT_MAX_STEPS,
    )
    timed_out = False

    def stop_agent() -> None:
        nonlocal timed_out
        timed_out = True
        conversation.pause()

    import threading

    timer = threading.Timer(config.agent_timeout_min * 60, stop_agent)
    try:
        timer.start()
        agent_summary, snapshot = _run_agent_for_review(
            conversation,
            task_prompt,
            repo_dir,
            base_sha,
            get_agent_final_response,
            lambda: timed_out,
        )
    finally:
        timer.cancel()
        if on_usage is not None:
            _report_usage(
                on_usage,
                "agent",
                conversation.conversation_stats.get_combined_metrics(),
            )

    changed_files = snapshot["changed_files"]
    violations = protected_path_violations(changed_files, config.protected_paths)
    if violations:
        raise RuntimeError(
            "The coding agent modified protected paths: " + ", ".join(violations)
        )
    ticket_result = results.get("gate", {}).get("result", {})
    fetched = results.get("fetch", {}).get("result", {})
    from backend.implementation_evaluation import evaluate_implementation

    evaluation = evaluate_implementation(
        llm=llm,
        repo_dir=repo_dir,
        config=config,
        ticket_key=run["ticket_key"],
        summary=ticket_result.get("summary") or fetched.get("summary", ""),
        description=ticket_result.get("description") or fetched.get("description", ""),
        changed_files=changed_files,
        diff=snapshot["proposed_patch"],
        on_usage=on_usage,
        on_event=(
            lambda message: on_agent_event("agent", "in-progress", message)
            if on_agent_event
            else None
        ),
    )
    return {
        **task_data,
        "phase": "review",
        "instructions": (
            "The coding agent prepared a proposal from the approved ticket and "
            "repository instructions. Review and edit each changed file below. "
            "Accept the final code to run validation, or provide feedback to ask "
            "the agent for another revision."
        ),
        "summary": summary,
        "agent_summary": redact(agent_summary[:4000], *secrets),
        "implementation_score": evaluation["score"],
        "implementation_evaluation": evaluation,
        "branch": branch,
        "base_sha": base_sha,
        **snapshot,
        "feedback": feedback,
        "test_results": "",
        "test_output": "",
    }


def apply_reviewed_files(
    run_id: str,
    config: PipelineConfig,
    task_data: dict,
    files: dict[str, str | None],
) -> None:
    """Apply only edits to files currently in the proposal, inside its workspace."""
    proposed_paths = {item["path"] for item in task_data.get("proposed_files", [])}
    if not proposed_paths or set(files) != proposed_paths:
        raise ValueError("Submit the complete set of files shown in the proposal")
    if any(not isinstance(path, str) for path in files):
        raise ValueError("Implementation file paths must be strings")

    _workspace, repo_dir = _repository_directory(run_id, config)
    base_sha = task_data.get("base_sha")
    branch = task_data.get("branch")
    if not repo_dir.is_dir() or not base_sha or not branch:
        raise ValueError("The implementation workspace is no longer available")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or ".." in branch.split("/"):
        raise ValueError("The saved implementation branch name is invalid")
    _run_checked(
        ["git", "checkout", "--force", branch],
        repo_dir,
        "Restoring implementation branch",
    )
    _run_checked(
        ["git", "reset", "--hard", base_sha],
        repo_dir,
        "Restoring target branch checkpoint",
    )

    baseline = set(
        _run_checked(
            ["git", "ls-tree", "-r", "--name-only", base_sha],
            repo_dir,
            "Reading target branch files",
        ).splitlines()
    )
    for relative_path in proposed_paths:
        normalized = relative_path.replace("\\", "/")
        pure_path = PurePosixPath(normalized)
        if (
            pure_path.is_absolute()
            or not normalized
            or any(part in ("", ".", "..", ".git") for part in pure_path.parts)
        ):
            raise ValueError(f"Invalid implementation path: {relative_path}")
        destination = repo_dir.joinpath(*pure_path.parts)
        try:
            destination.resolve(strict=False).relative_to(repo_dir.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Implementation path escapes the workspace: {relative_path}"
            ) from exc
        if destination.is_symlink():
            raise ValueError(
                f"Symbolic links cannot be edited in the review panel: {relative_path}"
            )
        if relative_path not in baseline and destination.exists():
            if destination.is_file():
                destination.unlink()
            else:
                raise ValueError(f"Expected a regular proposed file: {relative_path}")

    total_bytes = 0
    for relative_path, content in files.items():
        normalized = relative_path.replace("\\", "/")
        destination = repo_dir.joinpath(*PurePosixPath(normalized).parts)
        if content is None:
            if destination.exists():
                destination.unlink()
            continue
        if not isinstance(content, str):
            raise ValueError(f"Content for {relative_path} must be text or null")
        content = _trim_extra_blank_lines_at_eof(content)
        encoded = content.encode("utf-8")
        total_bytes += len(encoded)
        if total_bytes > MAX_MANUAL_FILE_BYTES:
            raise ValueError("Edited files exceed the manual editor size limit")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)


def get_review_snapshot(run_id: str, config: PipelineConfig, task_data: dict) -> dict:
    _workspace, repo_dir = _repository_directory(run_id, config)
    return review_snapshot(repo_dir, task_data["base_sha"])


def validate_and_commit(
    run_id: str,
    config: PipelineConfig,
    task_data: dict,
) -> dict:
    """Validate the reviewed working tree, then persist it as a local commit."""
    _workspace, repo_dir = _repository_directory(run_id, config)
    base_sha = task_data.get("base_sha")
    branch = task_data.get("branch")
    if not repo_dir.is_dir() or not base_sha or not branch:
        raise RuntimeError("The implementation workspace is no longer available")
    changed_files = _changed_files(repo_dir, base_sha)
    if not changed_files:
        raise RuntimeError("The reviewed implementation contains no changes")
    violations = protected_path_violations(changed_files, config.protected_paths)
    if violations:
        raise RuntimeError(
            "Verification cannot pass because protected paths were changed: "
            + ", ".join(violations)
        )

    diff_check = subprocess.run(
        ["git", "diff", "--check", base_sha],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    for _ in range(3):
        if diff_check.returncode == 0:
            break
        check_output = diff_check.stdout or diff_check.stderr
        normalized_paths = _normalize_reported_eof_blank_lines(
            repo_dir,
            changed_files,
            check_output,
        )
        if not normalized_paths:
            break
        diff_check = subprocess.run(
            ["git", "diff", "--check", base_sha],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    if diff_check.returncode != 0:
        raise RuntimeError(
            "Whitespace/conflict-marker check failed: "
            + (diff_check.stdout or diff_check.stderr)[:2000]
        )
    approved_files = _read_proposed_files(repo_dir, changed_files)

    test_results = "No test command is configured; git diff --check passed."
    test_output = ""
    if config.verify_mode == "command":
        if config.test_setup_command:
            setup_rc, setup_output = run_shell(
                config.test_setup_command,
                repo_dir,
                config.test_timeout_min * 60,
            )
            if setup_rc != 0:
                why = (
                    "timed out" if setup_rc is None else f"exited with code {setup_rc}"
                )
                raise RuntimeError(
                    f"Configured test setup {why}: {config.test_setup_command}\n"
                    f"{_tail(setup_output)}"
                )
        started = time.monotonic()
        test_rc, test_output = run_shell(
            config.test_command,
            repo_dir,
            config.test_timeout_min * 60,
        )
        elapsed = int(time.monotonic() - started)
        if test_rc == PYTEST_NO_TESTS_EXIT and "pytest" in config.test_command:
            test_results = (
                f"No tests were collected by `{config.test_command}`; "
                "git diff --check passed."
            )
        elif test_rc != 0:
            why = "timed out" if test_rc is None else f"exited with code {test_rc}"
            raise RuntimeError(
                f"Configured tests {why}: {config.test_command}\n{_tail(test_output)}"
            )
        else:
            test_results = f"`{config.test_command}` passed in {elapsed}s."
    elif config.verify_mode == "github_actions":
        test_results = (
            "Local diff check passed. GitHub Actions will run after a pull request "
            "is opened."
        )

    after_checks = review_snapshot(repo_dir, base_sha)
    if (
        after_checks["changed_files"] != changed_files
        or after_checks["proposed_files"] != approved_files
    ):
        raise RuntimeError(
            "Configured setup or tests modified repository files. Review the new "
            "changes and rerun validation before saving the implementation."
        )

    _run_checked(
        ["git", "config", "user.name", "SDLC Agent"],
        repo_dir,
        "Configuring implementation commit author",
    )
    _run_checked(
        ["git", "config", "user.email", "sdlc-agent@users.noreply.github.com"],
        repo_dir,
        "Configuring implementation commit author",
    )
    _run_checked(
        ["git", "add", "-A", "--", *changed_files],
        repo_dir,
        "Staging reviewed implementation",
    )
    summary = task_data.get("summary") or "Implement approved ticket changes"
    _run_checked(
        ["git", "commit", "-m", f"{run_id[:8]}: {summary[:120]}"],
        repo_dir,
        "Saving reviewed implementation",
    )
    commit_sha = _run_checked(
        ["git", "rev-parse", "HEAD"],
        repo_dir,
        "Recording implementation commit",
    )
    patch = _proposed_patch(repo_dir, base_sha)
    return {
        "summary": task_data.get("agent_summary") or summary,
        "branch": branch,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "changed_files": "\n".join(changed_files),
        "test_results": test_results,
        "test_output": redact(
            _tail(test_output, 30)[:6000],
            config.github_token,
            config.azure_key,
            config.ado_pat,
            config.jira_api_token,
        ),
        "diff": patch[:MAX_MANUAL_PATCH_BYTES],
        "source": "manual_agent",
    }
