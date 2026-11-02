"""Tests for app-scoped outbound run notifications."""

import logging
from email.message import EmailMessage

from backend import notifications


def _run(settings, status="success", **kwargs):
    notifications.send_run_notifications(
        settings=settings,
        app_id="checkout",
        app_name="Checkout",
        run_id="run-1",
        ticket_key="PAY-42",
        status=status,
        message="Run details",
        pr_url="https://github.com/example/repo/pull/1",
        **kwargs,
    )


def test_success_notifications_send_to_each_configured_app_destination(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notifications,
        "_send_slack",
        lambda url, text: calls.append(("slack", url, text)),
    )
    monkeypatch.setattr(
        notifications,
        "_send_teams",
        lambda url, text: calls.append(("teams", url, text)),
    )
    monkeypatch.setattr(
        notifications,
        "_send_email",
        lambda settings, text: calls.append(
            ("email", settings["NOTIFY_EMAIL_TO"], text)
        ),
    )
    settings = {
        "NOTIFY_ON_SUCCESS": "true",
        "SLACK_WEBHOOK_URL": "slack-url",
        "TEAMS_WEBHOOK_URL": "teams-url",
        "NOTIFY_EMAIL_TO": "team@example.com",
    }

    _run(settings)

    assert [call[0] for call in calls] == ["slack", "teams", "email"]
    assert all("PAY-42" in call[2] and "run-1" in call[2] for call in calls)
    assert all("pull/1" in call[2] for call in calls)


def test_webhook_senders_use_supported_payloads_and_timeouts(monkeypatch):
    posts = []

    class Response:
        def raise_for_status(self):
            return None

    def post(url, **kwargs):
        posts.append((url, kwargs))
        return Response()

    monkeypatch.setattr(notifications.requests, "post", post)
    notifications._send_slack("slack-url", "Run done")
    notifications._send_teams("teams-url", "Run done")

    assert posts[0] == (
        "slack-url",
        {"json": {"text": "Run done"}, "timeout": 10},
    )
    teams_payload = posts[1][1]["json"]
    assert posts[1][0] == "teams-url"
    assert posts[1][1]["timeout"] == 10
    assert teams_payload["attachments"][0]["contentType"] == (
        "application/vnd.microsoft.card.adaptive"
    )
    assert teams_payload["attachments"][0]["content"]["body"][0]["text"] == "Run done"


def test_success_and_failure_delivery_can_be_enabled_independently(monkeypatch):
    sent = []
    monkeypatch.setattr(
        notifications, "_send_slack", lambda _url, text: sent.append(text)
    )
    settings = {
        "NOTIFY_ON_SUCCESS": "false",
        "NOTIFY_ON_FAILURE": "true",
        "SLACK_WEBHOOK_URL": "slack-url",
    }

    _run(settings, status="success")
    assert sent == []
    _run(settings, status="human")
    assert len(sent) == 1 and "status: human" in sent[0]


def test_one_channel_failure_does_not_block_other_channels_or_leak_secret(
    monkeypatch, caplog
):
    secret = "https://hooks.slack.test/private-token"
    sent = []

    def fail(_url, _text):
        raise ConnectionError(f"Could not reach {secret}")

    monkeypatch.setattr(notifications, "_send_slack", fail)
    monkeypatch.setattr(
        notifications,
        "_send_teams",
        lambda _url, _text: sent.append("teams"),
    )
    monkeypatch.setattr(
        notifications,
        "_send_email",
        lambda _settings, _text: sent.append("email"),
    )
    caplog.set_level(logging.ERROR, logger=notifications.__name__)

    _run(
        {
            "NOTIFY_ON_FAILURE": "true",
            "SLACK_WEBHOOK_URL": secret,
            "TEAMS_WEBHOOK_URL": "teams-url",
            "NOTIFY_EMAIL_TO": "team@example.com",
        },
        status="error",
    )

    assert sent == ["teams", "email"]
    assert secret not in caplog.text
    assert caplog.records[0].channel == "slack"
    assert caplog.records[0].error_type == "ConnectionError"


def test_email_uses_smtp_settings_and_comma_separated_recipients(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, context):
            captured["tls_context"] = context

        def login(self, username, password):
            captured["login"] = (username, password)

        def send_message(self, email: EmailMessage):
            captured["email"] = email

    monkeypatch.setattr(
        notifications.smtplib,
        "SMTP",
        lambda *args, **kwargs: (
            captured.update(connection=(args, kwargs)) or FakeSMTP()
        ),
    )

    notifications._send_email(
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "2525",
            "SMTP_FROM": "bot@example.com",
            "SMTP_USERNAME": "bot",
            "SMTP_PASSWORD": "smtp-secret",
            "NOTIFY_EMAIL_TO": "one@example.com, two@example.com",
        },
        "Run finished",
    )

    assert captured["connection"] == (
        ("smtp.example.com", 2525),
        {"timeout": 10},
    )
    assert captured["login"] == ("bot", "smtp-secret")
    assert captured["email"]["To"] == "one@example.com, two@example.com"
    assert "Run finished" in captured["email"].get_content()
