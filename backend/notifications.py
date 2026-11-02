"""Failure-isolated outbound notifications for completed pipeline runs."""

import logging
import smtplib
import ssl
from email.message import EmailMessage

import requests


logger = logging.getLogger(__name__)


def _enabled(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Notification switches must be true or false")


def _message(
    app_name: str,
    run_id: str,
    ticket_key: str,
    status: str,
    message: str | None,
    pr_url: str | None,
) -> str:
    outcome = (
        "completed successfully"
        if status == "success"
        else f"finished with status: {status}"
    )
    lines = [
        f"SDLC run for {app_name} {outcome}.",
        f"Ticket: {ticket_key}",
        f"Run: {run_id}",
    ]
    if message:
        lines.append(f"Details: {message}")
    if pr_url:
        lines.append(f"Pull request: {pr_url}")
    return "\n".join(lines)


def _send_slack(webhook_url: str, text: str) -> None:
    response = requests.post(webhook_url, json={"text": text}, timeout=10)
    response.raise_for_status()


def _send_teams(webhook_url: str, text: str) -> None:
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }
        ],
    }
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()


def _send_email(settings: dict[str, str], text: str) -> None:
    host = settings.get("SMTP_HOST", "").strip()
    sender = settings.get("SMTP_FROM", "").strip()
    recipients = [
        address.strip()
        for address in settings.get("NOTIFY_EMAIL_TO", "").split(",")
        if address.strip()
    ]
    if not recipients:
        return
    if not host or not sender:
        raise ValueError("Email notifications require SMTP_HOST and SMTP_FROM")

    port = int(settings.get("SMTP_PORT") or "587")
    username = settings.get("SMTP_USERNAME", "").strip()
    password = settings.get("SMTP_PASSWORD", "")
    if username and not password:
        raise ValueError("SMTP_PASSWORD is required when SMTP_USERNAME is configured")

    email = EmailMessage()
    email["Subject"] = "SDLC pipeline run update"
    email["From"] = sender
    email["To"] = ", ".join(recipients)
    email.set_content(text)

    use_starttls = _enabled(settings.get("SMTP_STARTTLS"), default=True)
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if use_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if username:
            smtp.login(username, password)
        smtp.send_message(email)


def send_run_notifications(
    *,
    settings: dict[str, str],
    app_id: str,
    app_name: str,
    run_id: str,
    ticket_key: str,
    status: str,
    message: str | None,
    pr_url: str | None,
) -> None:
    """Send enabled notifications without allowing delivery errors to affect a run."""
    notify = _enabled(
        settings.get(
            "NOTIFY_ON_SUCCESS" if status == "success" else "NOTIFY_ON_FAILURE"
        )
    )
    if not notify:
        return

    text = _message(app_name, run_id, ticket_key, status, message, pr_url)
    destinations = (
        ("slack", settings.get("SLACK_WEBHOOK_URL", "").strip(), _send_slack),
        ("teams", settings.get("TEAMS_WEBHOOK_URL", "").strip(), _send_teams),
    )
    for channel, destination, sender in destinations:
        if not destination:
            continue
        try:
            sender(destination, text)
        except Exception as error:
            logger.error(
                "Outbound run notification delivery failed",
                extra={
                    "run_id": run_id,
                    "app_id": app_id,
                    "channel": channel,
                    "error_type": type(error).__name__,
                },
            )

    if settings.get("NOTIFY_EMAIL_TO", "").strip():
        try:
            _send_email(settings, text)
        except Exception as error:
            logger.error(
                "Outbound run notification delivery failed",
                extra={
                    "run_id": run_id,
                    "app_id": app_id,
                    "channel": "email",
                    "error_type": type(error).__name__,
                },
            )
