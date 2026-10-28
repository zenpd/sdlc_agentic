"""Ticket parsing and routing normalization for the fetch stage."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TicketDetails:
    summary: str
    status: str
    description: str
    target_repo: str | None
    target_branch: str | None
    persona: str | None


def parse_ticket(text: str) -> TicketDetails:
    """Parse the provider-neutral text returned by a ticket adapter."""
    summary = ""
    status = ""
    in_description = False
    description_lines: list[str] = []

    for line in text.strip().split("\n"):
        if line.startswith("Summary:"):
            summary = line.split(":", 1)[1].strip()
        elif line.startswith("Status:"):
            status = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            in_description = True
            description_lines.append(line.split(":", 1)[1].strip())
        elif (
            in_description
            and line.strip()
            and not any(
                line.startswith(prefix)
                for prefix in ("Key:", "Summary:", "Status:", "Priority:")
            )
        ):
            description_lines.append(line.strip())

    description = "\n".join(description_lines)
    repo_match = re.search(
        r"(?im)^\s*repo(?:sitory)?\s*:\s*(\S+)\s*$",
        description,
    )
    branch_match = re.search(r"(?im)^\s*branch\s*:\s*(\S+)\s*$", description)
    persona_match = re.search(r"(?im)^\s*persona\s*:\s*(\S+)\s*$", description)
    description = re.sub(
        r"(?im)^\s*(repo(?:sitory)?|branch|persona)\s*:\s*\S+\s*$",
        "",
        description,
    ).strip()

    return TicketDetails(
        summary=summary,
        status=status,
        description=description,
        target_repo=repo_match.group(1) if repo_match else None,
        target_branch=branch_match.group(1) if branch_match else None,
        persona=persona_match.group(1) if persona_match else None,
    )
