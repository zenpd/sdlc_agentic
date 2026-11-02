"""Publish a reviewed Manual implementation and create its GitHub pull request."""

from __future__ import annotations

import re
import subprocess
from urllib.parse import quote

from backend.jira_bot import PipelineConfig, gh_api, git_failure_message, redact
from backend.manual_implementation import _repository_directory


def _git_push(config: PipelineConfig, repo_dir, branch: str, commit_sha: str) -> None:
    if not config.github_token:
        raise RuntimeError("The configured GitHub integration has no repository token")
    remote_url = f"https://{config.github_token}@github.com/{config.target_repo}.git"
    try:
        result = subprocess.run(
            ["git", "push", remote_url, f"HEAD:refs/heads/{branch}"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = redact(str(exc), config.github_token)
        raise RuntimeError(
            f"Could not publish implementation branch: {detail}"
        ) from exc
    if result.returncode:
        message = git_failure_message(
            f"Publishing implementation branch {branch}",
            result.stderr or result.stdout,
            config.app_name,
            config.github_token,
        )
        raise RuntimeError(message)
    try:
        verify = subprocess.run(
            [
                "git",
                "ls-remote",
                remote_url,
                f"refs/heads/{branch}",
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = redact(str(exc), config.github_token)
        raise RuntimeError(f"Could not confirm the published branch: {detail}") from exc
    if verify.returncode:
        raise RuntimeError(
            "Could not confirm the published implementation branch with GitHub: "
            + git_failure_message(
                "Checking published branch",
                verify.stderr or verify.stdout,
                config.app_name,
                config.github_token,
            )
        )
    remote_sha = verify.stdout.split(maxsplit=1)[0] if verify.stdout.strip() else ""
    if remote_sha.lower() != commit_sha.lower():
        raise RuntimeError(
            "GitHub branch does not point to the human-approved implementation commit"
        )


def create_manual_pull_request(
    run_id: str,
    config: PipelineConfig,
    ticket_key: str,
    ticket_summary: str,
    ticket_description: str,
    implementation: dict,
    verification: dict,
) -> dict:
    """Push the saved commit and idempotently create or reuse its open PR."""
    if not re_full_repo(config.target_repo):
        raise ValueError(
            "Configured GitHub repository must use owner/repository format"
        )
    branch = implementation.get("branch", "")
    commit_sha = implementation.get("commit_sha", "")
    if not branch or not commit_sha:
        raise RuntimeError("Approved implementation branch or commit SHA is missing")
    _workspace, repo_dir = _repository_directory(run_id, config)
    if not repo_dir.is_dir():
        raise RuntimeError("The implementation workspace is unavailable")
    if _git_output(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir) != branch:
        raise RuntimeError("Implementation workspace is not on the approved branch")
    if _git_output(["rev-parse", "HEAD"], repo_dir) != commit_sha:
        raise RuntimeError(
            "Implementation workspace no longer matches the approved commit"
        )
    if _git_output(["status", "--porcelain"], repo_dir):
        raise RuntimeError(
            "Implementation workspace contains changes outside the approved commit"
        )

    _git_push(config, repo_dir, branch, commit_sha)
    head = quote(f"{config.target_repo.split('/')[0]}:{branch}", safe="")
    base = quote(config.target_branch, safe="")
    open_prs = gh_api(
        config.github_token,
        "GET",
        f"/repos/{config.target_repo}/pulls?state=open&head={head}&base={base}&per_page=100",
    )
    if not isinstance(open_prs, list):
        raise RuntimeError("GitHub returned an invalid pull request list")
    matching = [
        item
        for item in open_prs
        if isinstance(item, dict)
        and isinstance(item.get("head"), dict)
        and isinstance(item.get("base"), dict)
        and item["head"].get("ref") == branch
        and item["base"].get("ref") == config.target_branch
    ]
    if len(matching) > 1:
        raise RuntimeError("Multiple open pull requests already exist for this branch")
    if matching:
        pull_request = matching[0]
        creation_status = "existing"
    else:
        payload = {
            "title": f"{ticket_key}: {ticket_summary}"[:250],
            "head": branch,
            "base": config.target_branch,
            "body": (
                f"## Summary\n{ticket_summary}\n\n"
                f"## Approved requirements\n{ticket_description}\n\n"
                f"## Implementation\n{implementation.get('summary', '')}\n\n"
                f"## Verification\n{verification.get('review_report', '')}\n\n"
                f"Closes {ticket_key}"
            ),
        }
        response = gh_api(
            config.github_token,
            "POST",
            f"/repos/{config.target_repo}/pulls",
            payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("GitHub returned an invalid pull request response")
        pull_request = response
        creation_status = "created"

    pr_url = pull_request.get("html_url")
    pr_number = pull_request.get("number")
    head_data = pull_request.get("head")
    base_data = pull_request.get("base")
    if (
        pull_request.get("state") != "open"
        or not isinstance(pr_url, str)
        or not pr_url.startswith(f"https://github.com/{config.target_repo}/pull/")
        or isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or not isinstance(head_data, dict)
        or not isinstance(base_data, dict)
        or head_data.get("ref") != branch
        or base_data.get("ref") != config.target_branch
    ):
        raise RuntimeError(
            "GitHub did not confirm an open pull request for the approved branches"
        )

    evidence = (
        f"Pull request #{pr_number} {creation_status}; source {branch} -> "
        f"target {config.target_branch}. GitHub confirmed it is open."
    )
    return {
        "url": pr_url,
        "number": pr_number,
        "source_branch": branch,
        "target_branch": config.target_branch,
        "creation_status": creation_status,
        "state": "open",
        "evidence": evidence,
    }


def _git_output(command: list[str], cwd) -> str:
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
        detail = (result.stderr or result.stdout).strip()[:500]
        raise RuntimeError(f"Git {' '.join(command)} failed: {detail}")
    return result.stdout.strip()


def re_full_repo(repo: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        and all(part not in (".", "..") for part in repo.split("/"))
    )
