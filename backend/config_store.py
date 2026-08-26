"""Reads/writes the .env-backed integration settings that back Jira/ADO/
GitHub/Azure OpenAI config, so the Config tab never has to shell out to an
editor. Deliberately a plain line-editor rather than a full .env parser: it
only touches lines for keys it manages, leaves everything else (comments,
section headers, unrelated keys) exactly as-is, and appends managed keys
that don't exist yet at the end of the file.

Secret values are never returned by get_config() — only whether they're set.
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


ENV_PATH = Path(__file__).parent.parent / ".env"

# Fields safe to echo back verbatim in GET /api/config.
NON_SECRET_FIELDS = [
    "TICKET_PROVIDER",
    "JIRA_BASE_URL",
    "JIRA_USER_EMAIL",
    "ADO_ORG_URL",
    "ADO_PROJECT",
    "ADO_ASSIGNEE",
    "TARGET_REPO",
    "TARGET_BRANCH",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "STATUS_DONE",
    "STATUS_IN_PROGRESS",
    "STATUS_FAILED",
    "JIRA_JQL",
    "ADO_WIQL",
]

# Fields that are only ever reported as {"set": bool} — the raw value never
# leaves this process. A PUT with one of these omitted or blank leaves the
# stored value untouched (does not clear it).
SECRET_FIELDS = [
    "JIRA_API_TOKEN",
    "ADO_PAT",
    "GITHUB_TOKEN",
    "AZURE_OPENAI_API_KEY",
]

ALL_MANAGED_FIELDS = NON_SECRET_FIELDS + SECRET_FIELDS


def _read_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)


def get_config() -> dict:
    """Current config: non-secret fields verbatim, secret fields as
    {"set": bool} — never a raw secret value."""
    out: dict = {}
    for key in NON_SECRET_FIELDS:
        out[key] = os.getenv(key, "")
    for key in SECRET_FIELDS:
        val = os.getenv(key, "")
        # A hand-edited placeholder like "<your-ado-personal-access-token>"
        # is present but not actually configured — don't report it as set.
        is_placeholder = val.startswith("<") and val.endswith(">")
        out[key] = {"set": bool(val) and not is_placeholder}
    return out


def update_config(updates: dict[str, Optional[str]]) -> None:
    """Write the given keys to .env (only recognized managed keys; unknown
    keys are rejected rather than silently written), then reload the
    running process's os.environ so the next pipeline run picks it up.

    A key present with an empty/None value is skipped for SECRET_FIELDS
    (leaves the stored secret untouched — a blank PUT is never "clear my
    token"). For NON_SECRET_FIELDS an empty value means "reset to default":
    the key's line is removed entirely rather than written as `KEY=`, since
    jira_bot.py's config reads every one of these via `os.getenv(key,
    default)` — a *present-but-empty* value would win over that default
    (`os.getenv` only falls back on a genuinely missing key), silently
    pinning the field to "" instead of actually resetting it.
    """
    unknown = set(updates) - set(ALL_MANAGED_FIELDS)
    if unknown:
        raise ValueError(f"Unrecognized config field(s): {', '.join(sorted(unknown))}")

    to_set: dict[str, str] = {}
    to_clear: set[str] = set()
    for key, value in updates.items():
        if not value:
            if key in SECRET_FIELDS:
                continue  # blank secret field on a PUT = "leave it alone"
            to_clear.add(key)  # blank non-secret field = "reset to default"
        else:
            to_set[key] = value

    if not to_set and not to_clear:
        return

    lines = _read_lines()
    kept_lines = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            kept_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in to_clear:
            continue  # drop the line entirely
        if key in to_set:
            kept_lines.append(f"{key}={to_set[key]}\n")
            seen.add(key)
        else:
            kept_lines.append(line)

    for key, value in to_set.items():
        if key not in seen:
            if kept_lines and not kept_lines[-1].endswith("\n"):
                kept_lines.append("\n")
            kept_lines.append(f"{key}={value}\n")

    ENV_PATH.write_text("".join(kept_lines), encoding="utf-8")
    for key in to_clear:
        os.environ.pop(key, None)
    load_dotenv(ENV_PATH, override=True)
