"""Multi-application onboarding: per-app config store, inheritance from .env,
API wiring, and — most importantly — that two applications' pipelines running
at the same time each use only their own settings.

Every test runs against a throwaway SQLite file, never backend/runs.db.
"""

import threading
from types import SimpleNamespace

import pytest

from conftest import GLOBAL_ENV, client, sign_in
from backend import api_server, apps_store, jira_bot, runs_db
from backend.jira_bot import PipelineConfig, PipelineHalt


# ── PipelineConfig ───────────────────────────────────────────────────
def test_pipeline_config_defaults_follow_provider():
    ado = PipelineConfig.from_settings({"TICKET_PROVIDER": "ado"})
    jira = PipelineConfig.from_settings({})
    assert (ado.status_failed, ado.status_done, ado.status_in_progress) == ("New", "Closed", "Active")
    assert (jira.status_failed, jira.status_done, jira.status_in_progress) == ("Human In Loop", "Done", "In Progress")
    assert jira.branch_prefix == "feat/" and jira.gate_min_words == 5
    assert jira.azure_model == "azure/gpt-4.1-mini"


def test_pipeline_config_per_app_defaults_and_validation():
    cfg = PipelineConfig.from_settings({"BRANCH_PREFIX": "agent fix", "GATE_MIN_WORDS": "12", "DEFAULT_PERSONA": "security-reviewer"})
    assert cfg.branch_prefix == "agent-fix/"
    assert cfg.gate_min_words == 12
    assert cfg.default_persona == "security-reviewer"
    with pytest.raises(ValueError):
        PipelineConfig.from_settings({"TICKET_PROVIDER": "github-issues"})


def test_missing_settings_depend_on_provider():
    cfg = PipelineConfig.from_settings({"TICKET_PROVIDER": "jira", "GITHUB_TOKEN": "t"})
    assert "JIRA_API_TOKEN" in cfg.missing_settings()
    assert "ADO_PAT" not in cfg.missing_settings()


# ── apps_store ───────────────────────────────────────────────────────
def test_default_app_inherits_everything_from_env():
    apps = apps_store.list_apps()
    assert [a["id"] for a in apps] == ["default"]
    resolved = apps_store.resolve_settings("default")
    for key, value in GLOBAL_ENV.items():
        assert resolved[key] == value


def test_app_overrides_layer_on_top_of_env():
    app = apps_store.create_app("Checkout Service", "Payments", config={
        "TARGET_REPO": "payments/checkout",
        "GITHUB_TOKEN": "checkout-token",
        "DEFAULT_PERSONA": "security-reviewer",
    })
    assert app["id"] == "checkout-service"
    resolved = apps_store.resolve_settings(app["id"])
    assert resolved["TARGET_REPO"] == "payments/checkout"
    assert resolved["GITHUB_TOKEN"] == "checkout-token"
    assert resolved["ADO_PAT"] == "global-pat"  # not overridden -> inherited
    assert apps_store.resolve_settings("default")["TARGET_REPO"] == "global/repo"  # untouched


def test_app_config_never_exposes_secret_values():
    apps_store.create_app("Billing", config={"ADO_PAT": "super-secret-billing-pat"})
    cfg = apps_store.get_app_config("billing")
    assert "super-secret-billing-pat" not in repr(cfg)
    assert cfg["secrets"]["ADO_PAT"] == {"set": True, "inherited": False}
    assert cfg["secrets"]["GITHUB_TOKEN"] == {"set": True, "inherited": True}


def test_blank_secret_kept_and_blank_setting_reverts_to_inherited():
    apps_store.create_app("HR Portal", config={"ADO_PAT": "hr-pat", "TARGET_BRANCH": "main"})
    apps_store.update_app_config("hr-portal", {"ADO_PAT": "", "TARGET_BRANCH": ""})
    resolved = apps_store.resolve_settings("hr-portal")
    assert resolved["ADO_PAT"] == "hr-pat"
    assert resolved["TARGET_BRANCH"] == "dev"


def test_validation_slugs_and_default_protection():
    with pytest.raises(ValueError):
        apps_store.create_app("Bad", config={"TICKET_PROVIDER": "trello"})
    assert not apps_store.app_exists("bad")  # rolled back
    with pytest.raises(ValueError):
        apps_store.create_app("Bad repo", config={"TARGET_REPO": "not a repo"})
    with pytest.raises(ValueError):
        apps_store.delete_app("default")
    assert apps_store.create_app("Same Name")["id"] == "same-name"
    assert apps_store.create_app("Same Name")["id"] == "same-name-2"


# ── API ──────────────────────────────────────────────────────────────
def test_api_onboarding_flow():
    sign_in("priya")
    r = client.post("/api/apps", json={"name": "Checkout Service", "team": "Payments",
                                       "config": {"TICKET_PROVIDER": "jira", "JIRA_BASE_URL": "https://x.atlassian.net"}})
    assert r.status_code == 200, r.text
    assert r.json()["ticket_provider"] == "jira"
    assert [a["id"] for a in client.get("/api/apps").json()] == ["default", "checkout-service"]

    r = client.put("/api/apps/checkout-service/config", json={"GATE_MIN_WORDS": "10", "JIRA_API_TOKEN": "tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["GATE_MIN_WORDS"] == "10"
    assert body["secrets"]["JIRA_API_TOKEN"]["set"] is True
    assert "tok" not in r.text

    assert client.put("/api/apps/checkout-service/config", json={"GATE_MIN_WORDS": "lots"}).status_code == 400
    assert client.get("/api/apps/nope/config").status_code == 404
    assert client.delete("/api/apps/default").status_code == 400
    assert client.delete("/api/apps/checkout-service").status_code == 200


def test_run_ticket_rejects_unknown_app():
    sign_in("priya")
    assert client.post("/api/run-ticket", json={"ticket_key": "1", "app_id": "ghost"}).status_code == 404


def test_runs_and_stats_are_scoped_per_app():
    sign_in("priya")
    apps_store.create_app("Billing")
    runs_db.create_run("r1", "100", "2026-10-07T10:00:00+00:00", "default")
    runs_db.create_run("r2", "200", "2026-10-07T10:01:00+00:00", "billing")
    runs_db.finish_run("r2", "success", "https://github.com/x/y/pull/1", 30)
    assert [r["id"] for r in client.get("/api/runs", params={"app_id": "billing"}).json()] == ["r2"]
    assert client.get("/api/stats", params={"app_id": "billing"}).json()["success"] == 1
    assert client.get("/api/stats", params={"app_id": "default"}).json()["success"] == 0
    assert client.get("/api/stats").json()["total"] == 2


# ── Concurrency: the bug this whole change exists to fix ──────────────
class _FakeTicketExecutor:
    """Records which credentials it was built with and which status it was
    asked to set; returns a deliberately too-short ticket so the run halts at
    the clarity gate (no LLM, git, or network involved)."""

    calls: list = []
    barrier: threading.Barrier

    def __init__(self, provider, **creds):
        self.provider, self.creds = provider, creds

    def __call__(self, action):
        if action.command == "get_ticket":
            # Make both pipelines be mid-flight at the same moment.
            _FakeTicketExecutor.barrier.wait(timeout=10)
            return SimpleNamespace(is_error=False, result=action.ticket_key or "T",
                                   text=f"Key: {action.ticket_key}\nSummary: Fix it\nStatus: New\nDescription: too short")
        if action.command == "update_status":
            _FakeTicketExecutor.calls.append((self.provider, self.creds, action.target_status))
        return SimpleNamespace(is_error=False, result="", text="ok")


def test_two_apps_running_at_once_never_share_settings(monkeypatch):
    monkeypatch.setattr(jira_bot, "ADOExecutor", lambda **kw: _FakeTicketExecutor("ado", **kw))
    monkeypatch.setattr(jira_bot, "JiraExecutor", lambda **kw: _FakeTicketExecutor("jira", **kw))
    _FakeTicketExecutor.calls = []
    _FakeTicketExecutor.barrier = threading.Barrier(2)

    apps_store.create_app("Checkout", config={"ADO_PAT": "checkout-pat", "STATUS_FAILED": "Needs Info",
                                             "DEFAULT_PERSONA": "security-reviewer"})
    apps_store.create_app("Billing", config={"TICKET_PROVIDER": "jira", "JIRA_BASE_URL": "https://billing.atlassian.net",
                                            "JIRA_USER_EMAIL": "bot@billing", "JIRA_API_TOKEN": "billing-token",
                                            "DEFAULT_PERSONA": "backend-engineer"})

    results: dict = {}
    fetch_logs: dict = {}

    def run(app_id, ticket):
        cfg = api_server._pipeline_config(app_id)

        def on_step(step, state, log=None):
            if step == "fetch" and log:
                fetch_logs[app_id] = log

        try:
            jira_bot.run_pipeline(ticket_key=ticket, on_step=on_step, config=cfg)
        except PipelineHalt as e:
            results[app_id] = e.status

    threads = [threading.Thread(target=run, args=("checkout", "11")),
               threading.Thread(target=run, args=("billing", "BILL-22"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results == {"checkout": "human", "billing": "human"}
    by_provider = {p: (creds, status) for p, creds, status in _FakeTicketExecutor.calls if status not in ("Active", "In Progress")}
    assert by_provider["ado"] == ({"org_url": "https://dev.azure.com/global-org", "project": "Global-Project",
                                   "pat": "checkout-pat", "assignee": ""}, "Needs Info")
    assert by_provider["jira"] == ({"base_url": "https://billing.atlassian.net", "user_email": "bot@billing",
                                    "api_token": "billing-token"}, "Human In Loop")
    assert "persona: security-reviewer" in fetch_logs["checkout"]
    assert "persona: backend-engineer" in fetch_logs["billing"]
