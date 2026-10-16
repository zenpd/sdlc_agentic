"""new_req.md point 3 (team-scoped applications) and point 11 (real tests / CI)."""

import os
import sys

import pytest

from conftest import client, sign_in
from backend import apps_store, jira_bot, runs_db, teams_store, users_store
from backend.jira_bot import PipelineHalt
from test_account_governance import _cfg, fake_world  # noqa: F401  (fixture)


# ── Point 3: teams & visibility ──────────────────────────────────────
@pytest.fixture
def two_teams():
    """nikhil (admin), priya in Payments, rahul in HR; one app per team + the shared default."""
    users_store.ensure_user("nikhil")  # first user -> admin
    for login in ("priya", "rahul"):
        users_store.ensure_user(login)
        users_store.set_role(login, "maintainer")
    payments, hr = teams_store.create_team("Payments"), teams_store.create_team("HR")
    teams_store.add_member(payments["id"], "priya")
    teams_store.add_member(hr["id"], "rahul")
    apps_store.create_app("Checkout", team_id=payments["id"])
    apps_store.create_app("HR Portal", team_id=hr["id"])
    runs_db.create_run("pay-run", "701", "2026-10-15T10:00:00+00:00", "checkout", triggered_by="priya")
    runs_db.create_run("hr-run", "702", "2026-10-15T10:01:00+00:00", "hr-portal", triggered_by="rahul")
    return payments, hr


def _app_ids():
    return [a["id"] for a in client.get("/api/apps").json()]


def test_free_text_team_becomes_a_real_team():
    app = apps_store.create_app("Billing", "Finance")
    assert app["team"] == "Finance" and app["team_id"] == "finance"
    assert [t["name"] for t in teams_store.list_teams()] == ["Finance"]


def test_members_only_see_their_teams_apps(two_teams):
    sign_in("priya")
    assert _app_ids() == ["default", "checkout"]
    sign_in("rahul")
    assert _app_ids() == ["default", "hr-portal"]
    sign_in("nikhil")
    assert _app_ids() == ["default", "checkout", "hr-portal"]


def test_hidden_app_is_404_everywhere(two_teams):
    sign_in("priya")
    assert client.get("/api/apps/hr-portal").status_code == 404
    assert client.get("/api/apps/hr-portal/config").status_code == 404
    assert client.put("/api/apps/hr-portal/config", json={"BRANCH_PREFIX": "x/"}).status_code == 404
    assert client.post("/api/run-ticket", json={"ticket_key": "1", "app_id": "hr-portal"}).status_code == 404
    assert client.get("/api/runs", params={"app_id": "hr-portal"}).status_code == 404
    assert client.get("/api/run-status/hr-run").status_code == 404
    # "All applications" views only include what she can see
    assert [r["id"] for r in client.get("/api/runs").json()] == ["pay-run"]
    assert client.get("/api/stats").json()["total"] == 1


def test_team_management_is_admin_only(two_teams):
    payments, hr = two_teams
    sign_in("priya")
    assert [t["name"] for t in client.get("/api/teams").json()] == ["Payments"]  # only her own
    assert client.post("/api/teams", json={"name": "Ops"}).status_code == 403
    assert client.post(f"/api/teams/{hr['id']}/members", json={"login": "priya"}).status_code == 403

    sign_in("nikhil")
    assert {t["name"] for t in client.get("/api/teams").json()} == {"Payments", "HR"}
    assert client.post("/api/teams", json={"name": "payments"}).status_code == 400  # duplicate (case-insensitive)
    assert client.post(f"/api/teams/{hr['id']}/members", json={"login": "ghost"}).status_code == 400
    r = client.post(f"/api/teams/{hr['id']}/members", json={"login": "@priya"})
    assert r.status_code == 200 and r.json()["members"] == ["priya", "rahul"]
    assert client.delete(f"/api/teams/{hr['id']}").status_code == 400  # still owns HR Portal

    sign_in("priya")
    assert "hr-portal" in _app_ids()  # joined HR -> sees its app


def test_moving_an_app_between_teams(two_teams):
    payments, hr = two_teams
    sign_in("priya")  # maintainer, but moving apps is admin-only
    assert client.patch("/api/apps/checkout", json={"team_id": hr["id"]}).status_code == 403
    assert client.patch("/api/apps/checkout", json={"description": "Payments checkout"}).status_code == 200

    sign_in("nikhil")
    r = client.patch("/api/apps/checkout", json={"team_id": hr["id"]})
    assert r.status_code == 200 and r.json()["team"] == "HR"
    sign_in("priya")
    assert "checkout" not in _app_ids()
    sign_in("nikhil")
    assert client.patch("/api/apps/checkout", json={"team_id": None}).json()["team"] == ""  # shared
    sign_in("rahul")
    assert "checkout" in _app_ids()


def test_active_team_preference(two_teams):
    payments, hr = two_teams
    sign_in("priya")
    assert client.put("/api/me/preferences", json={"active_team_id": payments["id"]}).status_code == 200
    assert client.put("/api/me/preferences", json={"active_team_id": hr["id"]}).status_code == 400
    assert client.put("/api/me/preferences", json={"default_app_id": "hr-portal"}).status_code == 400


# ── Point 11: running the app's own commands ─────────────────────────
PY = f'"{sys.executable}"'


def test_run_shell_success_failure_and_timeout(tmp_path):
    assert jira_bot.run_shell(f'{PY} -c "print(42)"', tmp_path, 30) == (0, "42\n")
    rc, out = jira_bot.run_shell(f'{PY} -c "import sys; print(\'boom\'); sys.exit(3)"', tmp_path, 30)
    assert rc == 3 and "boom" in out
    rc, _ = jira_bot.run_shell(f'{PY} -c "import time; time.sleep(30)"', tmp_path, 1)
    assert rc is None  # killed, not left running


def test_repo_commands_never_see_our_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("ADO_PAT", "pat_should_not_leak")
    rc, out = jira_bot.run_shell(f'{PY} -c "import os; print(os.getenv(\'GITHUB_TOKEN\'), os.getenv(\'ADO_PAT\'))"', tmp_path, 30)
    assert rc == 0 and out.strip() == "None None"


@pytest.fixture
def shell(monkeypatch):
    """Fake run_shell: scripted (exit code, output) per command."""
    calls, results = [], {}
    def fake(command, cwd, timeout_sec):
        calls.append(command)
        return results.get(command, (0, "ok"))
    monkeypatch.setattr(jira_bot, "run_shell", fake)
    return calls, results


def _capture_prompt(monkeypatch):
    import openhands.sdk as sdk
    sent = []

    class Conv:
        def __init__(self, **_): pass
        def send_message(self, m): sent.append(m)
        def run(self): pass
    monkeypatch.setattr(sdk, "Conversation", Conv)
    return sent


def _capture_pr_body(monkeypatch, extra=None):
    bodies = []

    def fake_gh(token, method, path, json_data=None):
        if path.endswith("/pulls"):
            bodies.append(json_data["body"])
            return {"html_url": "https://github.com/o/r/pull/9"}
        return (extra or (lambda *a: {}))(method, path, json_data)
    monkeypatch.setattr(jira_bot, "gh_api", fake_gh)
    return bodies


def test_command_mode_tests_pass(fake_world, shell, monkeypatch):
    calls, _ = shell
    prompts = _capture_prompt(monkeypatch)
    bodies = _capture_pr_body(monkeypatch)
    result = jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="command", TEST_SETUP_COMMAND="pip install -r requirements.txt", TEST_COMMAND="pytest -q"))
    assert result["status"] == "success"
    assert calls == ["pip install -r requirements.txt", "pytest -q"]  # setup before agent, tests after
    assert "TESTS: this repository is tested with `pytest -q`" in prompts[0]
    assert "`pytest -q` passed" in bodies[0] and "Tests passing: [OK]" not in bodies[0]
    assert "pytest -q` passed" in fake_world.ticket.comments[-1]


def test_command_mode_failing_tests_block_the_pr(fake_world, shell):
    _, results = shell
    results["npm test"] = (1, "\n".join(f"line {i}" for i in range(40)) + "\n1 failing")
    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="command", TEST_COMMAND="npm test"))
    assert halt.value.status == "human" and "exit code 1" in halt.value.message
    assert not fake_world.pushed
    assert "1 failing" in fake_world.ticket.comments[-1]  # tail of the output goes to the ticket


def test_command_mode_setup_failure_and_timeout(fake_world, shell):
    _, results = shell
    results["make deps"] = (2, "no rule")
    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="command", TEST_SETUP_COMMAND="make deps", TEST_COMMAND="pytest"))
    assert halt.value.status == "failed" and "couldn't be installed" in halt.value.message

    results.clear()
    results["pytest"] = (None, "")
    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="command", TEST_COMMAND="pytest", TEST_TIMEOUT_MIN="3"))
    assert "timed out after 3 min" in halt.value.message


def test_command_mode_needs_a_command():
    with pytest.raises(ValueError):
        apps_store.create_app("No tests", config={"VERIFY_MODE": "command"})
    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="command"))
    assert "TEST_COMMAND" in halt.value.message


# ── Point 11: waiting for GitHub Actions ─────────────────────────────
def _ci(monkeypatch, check_runs, statuses=(), dispatched=None):
    monkeypatch.setattr(jira_bot, "CI_POLL_SEC", 0)
    monkeypatch.setattr(jira_bot, "CI_FIRST_CHECK_GRACE_SEC", 0.05)

    def extra(method, path, json_data):
        if "/check-runs" in path:
            return {"check_runs": check_runs}
        if path.endswith("/status"):
            return {"statuses": list(statuses)}
        if "/dispatches" in path and dispatched is not None:
            dispatched.append((path, json_data))
        return {}
    return _capture_pr_body(monkeypatch, extra)


def test_ci_mode_passes(fake_world, monkeypatch):
    dispatched = []
    _ci(monkeypatch, [{"name": "build", "status": "completed", "conclusion": "success"},
                      {"name": "lint", "status": "completed", "conclusion": "skipped"}], dispatched=dispatched)
    result = jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="github_actions", CI_WORKFLOW="ci.yml"))
    assert result["status"] == "success"
    assert dispatched and dispatched[0][0].endswith("/actions/workflows/ci.yml/dispatches")
    assert "GitHub Actions passed (2 checks)" in fake_world.ticket.comments[-1]


def test_ci_mode_failure_keeps_the_pr_link(fake_world, monkeypatch):
    _ci(monkeypatch, [{"name": "build", "status": "completed", "conclusion": "success"},
                      {"name": "unit-tests", "status": "completed", "conclusion": "failure"}])
    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="github_actions"))
    assert halt.value.status == "human" and "unit-tests" in halt.value.message
    assert halt.value.pr_url == "https://github.com/o/r/pull/9"
    assert fake_world.ticket.statuses[-1] == "New"


def test_ci_mode_no_checks_ever_ran(fake_world, monkeypatch):
    _ci(monkeypatch, [])
    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(VERIFY_MODE="github_actions"))
    assert "No CI checks ran" in halt.value.message


def test_failed_ci_run_still_records_the_pr(monkeypatch):
    from backend import api_server

    def halting_pipeline(**_):
        raise PipelineHalt("human", "CI failed", pr_url="https://github.com/o/r/pull/9")
    monkeypatch.setattr(api_server, "run_pipeline", halting_pipeline)
    runs_db.create_run("ci-run", "701", "2026-10-15T10:00:00+00:00")
    api_server._execute("ci-run", "701", _cfg())
    run = runs_db.get_run("ci-run")
    assert run["status"] == "human" and run["pr_url"].endswith("/pull/9")


# ── regression: every per-app setting must survive a save through the API ──
def test_every_app_setting_round_trips_through_the_api():
    sign_in("nikhil")  # admin
    samples = {"TICKET_PROVIDER": "ado", "TARGET_REPO": "o/r", "GATE_MIN_WORDS": "7", "REQUIRE_PR_APPROVAL": "true",
               "VERIFY_MODE": "command", "TEST_COMMAND": "pytest -q", "TEST_TIMEOUT_MIN": "5", "CI_WORKFLOW": "ci.yml"}
    body = {k: samples.get(k, f"value-{k.lower()}") for k in apps_store.SETTING_FIELDS}
    r = client.put("/api/apps/default/config", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["values"] == body
    # an unknown field is rejected loudly instead of being ignored
    assert client.put("/api/apps/default/config", json={"NOT_A_SETTING": "x"}).status_code == 422
