"""new_req.md point 4 (per-user preferences, recent runs, session management)
and point 10 (PR approval gate, protected-path guardrail)."""

import threading
import time
from types import SimpleNamespace

import pytest

from conftest import client, sign_in
from backend import api_server, apps_store, audit_store, jira_bot, oauth_store, runs_db, users_store
from backend.jira_bot import PipelineConfig, PipelineHalt, protected_path_violations


# ── Point 4: preferences ─────────────────────────────────────────────
def test_preferences_follow_the_user():
    sign_in("priya")
    apps_store.create_app("Checkout")
    r = client.put("/api/me/preferences", json={"default_app_id": "checkout", "theme": "dark"})
    assert r.status_code == 200 and r.json() == {"default_app_id": "checkout", "theme": "dark"}
    assert client.get("/api/auth/github/me").json()["preferences"] == {"default_app_id": "checkout", "theme": "dark"}

    assert client.put("/api/me/preferences", json={"theme": "neon"}).status_code == 400
    assert client.put("/api/me/preferences", json={"default_app_id": "ghost"}).status_code == 400
    client.put("/api/me/preferences", json={"theme": ""})  # blank -> back to default
    assert client.get("/api/auth/github/me").json()["preferences"] == {"default_app_id": "checkout"}


def test_mine_lists_only_my_runs_across_apps():
    sign_in("priya")
    apps_store.create_app("Billing")
    runs_db.create_run("r1", "100", "2026-10-14T10:00:00+00:00", "default", triggered_by="priya")
    runs_db.create_run("r2", "200", "2026-10-14T10:01:00+00:00", "billing", triggered_by="priya")
    runs_db.create_run("r3", "300", "2026-10-14T10:02:00+00:00", "billing", triggered_by="rahul")
    assert [r["id"] for r in client.get("/api/runs", params={"mine": "true"}).json()] == ["r2", "r1"]


# ── Point 4: session management ──────────────────────────────────────
def _session_for(login, ua):
    return oauth_store.create_session("tok", login, None, ua)


def test_sessions_list_revoke_and_sign_out_others():
    sign_in("priya")
    current = client.cookies.get("sdlc_session")
    laptop = _session_for("priya", "Mozilla/5.0 (Windows NT 10.0) Chrome/130")
    phone = _session_for("priya", "Mozilla/5.0 (iPhone) Safari/17")
    rahul = _session_for("rahul", "curl/8")

    sessions = client.get("/api/me/sessions").json()
    assert len(sessions) == 3 and sum(s["current"] for s in sessions) == 1
    raw = repr(sessions)
    assert current not in raw and laptop not in raw  # only public ids leave the server

    # revoke one specific other session
    laptop_id = oauth_store.public_id(laptop)
    assert client.delete(f"/api/me/sessions/{laptop_id}").status_code == 200
    assert oauth_store.get_session(laptop) is None
    # can't revoke someone else's session
    assert client.delete(f"/api/me/sessions/{oauth_store.public_id(rahul)}").status_code == 404

    # sign out everywhere else keeps this browser signed in
    assert client.post("/api/me/sessions/sign-out-others").json() == {"signed_out": 1}
    assert oauth_store.get_session(phone) is None
    assert oauth_store.get_session(current) is not None
    assert oauth_store.get_session(rahul) is not None


# ── Point 10: guardrail matching ─────────────────────────────────────
@pytest.mark.parametrize("files,patterns,blocked", [
    (["ci/deploy.yml"], ("ci/*",), ["ci/deploy.yml"]),
    (["services/ci/run.sh"], ("*/ci/*",), ["services/ci/run.sh"]),
    (["ci/run.sh"], ("*/ci/*",), ["ci/run.sh"]),                   # root-level folder too
    ([".github/workflows/build.yml"], (".github/workflows/**",), [".github/workflows/build.yml"]),
    (["infra/main.tf", "src/app.py"], ("*.tf",), ["infra/main.tf"]),
    (["infra/x.yaml"], ("infra/",), ["infra/x.yaml"]),
    (["src/app.py", "README.md"], ("ci/*", "*.tf"), []),
])
def test_protected_path_matching(files, patterns, blocked):
    assert protected_path_violations(files, patterns) == blocked


def test_protected_paths_parse_from_app_settings():
    cfg = PipelineConfig.from_settings({"PROTECTED_PATHS": "ci/*, *.tf\n.github/workflows/*", "REQUIRE_PR_APPROVAL": "TRUE"})
    assert cfg.protected_paths == ("ci/*", "*.tf", ".github/workflows/*")
    assert cfg.require_pr_approval is True
    assert PipelineConfig.from_settings({}).require_pr_approval is False
    with pytest.raises(ValueError):
        apps_store.create_app("Bad", config={"REQUIRE_PR_APPROVAL": "maybe"})


# ── Point 10: the pipeline really enforces both ──────────────────────
class _FakeTicket:
    def __init__(self, **_):
        self.statuses, self.comments = [], []

    def __call__(self, action):
        if action.command == "get_ticket":
            return SimpleNamespace(is_error=False, result="701", text=(
                "Key: 701\nSummary: Add health endpoint\nStatus: New\n"
                "Description: Add a GET /health endpoint to src/app.py that returns status ok as JSON"))
        if action.command == "update_status":
            self.statuses.append(action.target_status)
        if action.command == "add_comment":
            self.comments.append(action.comment_text)
        return SimpleNamespace(is_error=False, result="", text="ok")


class FakeConversation:
    conversation_stats = SimpleNamespace(get_combined_metrics=lambda: None)
    def __init__(self, **_): pass
    def send_message(self, _): pass
    def run(self): pass
    def pause(self): pass


@pytest.fixture
def fake_world(monkeypatch):
    """Everything outside our code is faked: ticket board, LLM, agent, git, GitHub."""
    import openhands.sdk as sdk
    from openhands.sdk.llm import TextContent

    world = SimpleNamespace(changed=["src/app.py"], pushed=False, ticket=_FakeTicket())
    monkeypatch.setattr(jira_bot, "ADOExecutor", lambda **kw: world.ticket)

    class FakeLLM:
        metrics = None
        def __init__(self, **_): pass
        def completion(self, messages):
            return SimpleNamespace(message=SimpleNamespace(content=[TextContent(text="YES clear enough")]))

    monkeypatch.setattr(sdk, "LLM", FakeLLM)
    monkeypatch.setattr(sdk, "Agent", lambda **_: None)
    monkeypatch.setattr(sdk, "Conversation", FakeConversation)
    monkeypatch.setattr(jira_bot, "_report_usage", lambda *a: None)
    # The read-only verification reviewer and its diff (dev_ruchitha's verify stage).
    monkeypatch.setattr(jira_bot, "_collect_verification_diff", lambda repo_dir, base: "diff --git a/src/app.py b/src/app.py\n")
    monkeypatch.setattr(jira_bot, "_run_verification_agent", lambda *a, **k: "VERDICT: PASS\nLooks right.")
    real_subprocess_run = jira_bot.subprocess.run

    def fake_subprocess_run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "diff", "--check"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_subprocess_run(cmd, *args, **kwargs)

    monkeypatch.setattr(jira_bot.subprocess, "run", fake_subprocess_run)

    def fake_run_cmd(cmd, cwd=None, silent=False):
        joined = " ".join(cmd)
        if "rev-parse" in joined:
            return 0, "base123\n", ""
        if "diff --name-only base123 HEAD" in joined:
            return 0, "\n".join(world.changed) + "\n", ""
        if "diff --stat" in joined:
            return 0, f" {len(world.changed)} file(s) changed, 9 insertions(+)\n", ""
        if cmd[:2] == ["git", "push"]:
            world.pushed = True
        return 0, "", ""

    monkeypatch.setattr(jira_bot, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(jira_bot, "gh_api", lambda *a, **k: {"html_url": "https://github.com/o/r/pull/9"})
    return world


def _cfg(**settings):
    base = {"TICKET_PROVIDER": "ado", "ADO_ORG_URL": "https://dev.azure.com/o", "ADO_PROJECT": "P", "ADO_PAT": "x",
            "GITHUB_TOKEN": "t", "AZURE_OPENAI_API_KEY": "k", "AZURE_OPENAI_ENDPOINT": "https://e",
            "AZURE_OPENAI_DEPLOYMENT": "gpt", "TARGET_REPO": "o/r"}
    return PipelineConfig.from_settings({**base, **settings}, app_id="checkout", app_name="Checkout")


def test_guardrail_blocks_protected_change_before_push(fake_world):
    fake_world.changed = ["src/app.py", ".github/workflows/deploy.yml"]
    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(PROTECTED_PATHS=".github/workflows/*"))
    assert halt.value.status == "human" and ".github/workflows/deploy.yml" in halt.value.message
    assert not fake_world.pushed
    assert fake_world.ticket.statuses[-1] == "New"  # handed back
    assert "Guardrail blocked" in fake_world.ticket.comments[-1]


def test_approval_gate_rejected_means_no_pr(fake_world):
    asked = {}

    def reject(summary):
        asked.update(summary)
        return False, "priya", "touches billing"

    with pytest.raises(PipelineHalt) as halt:
        jira_bot.run_pipeline("701", config=_cfg(REQUIRE_PR_APPROVAL="true"), wait_for_approval=reject)
    assert "rejected by @priya: touches billing" in halt.value.message
    assert asked["modified_files"] == ["src/app.py"]
    assert not fake_world.pushed


def test_approval_gate_approved_opens_pr(fake_world):
    result = jira_bot.run_pipeline("701", config=_cfg(REQUIRE_PR_APPROVAL="true"),
                                   wait_for_approval=lambda _: (True, "priya", ""))
    assert result["pr_url"].endswith("/pull/9") and fake_world.pushed


def test_no_approval_needed_by_default(fake_world):
    result = jira_bot.run_pipeline("701", config=_cfg())
    assert result["status"] == "success" and fake_world.pushed


# ── Point 10: the approval API and the waiting run ───────────────────
def _park_run_waiting(monkeypatch, timeout_sec=10):
    """Start _execute with a fake pipeline that just asks for approval."""
    monkeypatch.setattr(api_server, "_APPROVAL_POLL_SEC", 0.05)
    monkeypatch.setattr(api_server, "APPROVAL_TIMEOUT_SEC", timeout_sec)
    outcome = {}

    def fake_pipeline(ticket_key, on_step, config, wait_for_approval, **_):
        outcome["decision"] = wait_for_approval({"modified_files": ["src/app.py"]})
        return {"pr_url": None}

    monkeypatch.setattr(api_server, "run_pipeline", fake_pipeline)
    runs_db.create_run("run-1", "701", "2026-10-14T10:00:00+00:00", "default", triggered_by="priya")
    thread = threading.Thread(target=api_server._execute, args=("run-1", "701", _cfg()))
    thread.start()
    for _ in range(100):
        if runs_db.get_run("run-1")["status"] == "awaiting-approval":
            break
        time.sleep(0.02)
    return thread, outcome


def test_maintainer_approves_waiting_run(monkeypatch):
    users_store.ensure_user("first-admin")
    thread, outcome = _park_run_waiting(monkeypatch)
    assert runs_db.get_run("run-1")["approval"]["status"] == "pending"

    sign_in("rahul", "runner")
    assert client.post("/api/runs/run-1/approval", json={"decision": "approve"}).status_code == 403

    sign_in("priya", "maintainer")
    r = client.post("/api/runs/run-1/approval", json={"decision": "approve", "note": "looks good"})
    assert r.status_code == 200 and r.json()["approval"]["by"] == "priya"
    thread.join(timeout=5)
    assert outcome["decision"] == (True, "priya", "looks good")
    assert runs_db.get_run("run-1")["status"] == "success"
    # one decision only
    assert client.post("/api/runs/run-1/approval", json={"decision": "reject"}).status_code == 409
    entry = audit_store.list_entries()[0]
    assert (entry["actor"], entry["action"], entry["details"]["note"]) == ("priya", "run.approve", "looks good")


def test_approval_times_out(monkeypatch):
    thread, outcome = _park_run_waiting(monkeypatch, timeout_sec=0.3)
    thread.join(timeout=5)
    assert outcome["decision"] == (None, "", "")
    assert runs_db.get_run("run-1")["approval"]["status"] == "expired"


def test_restart_does_not_leave_runs_waiting_forever():
    runs_db.create_run("stale", "701", "2026-10-14T10:00:00+00:00")
    runs_db.request_approval("stale")
    runs_db.init_db()  # what a server restart does
    run = runs_db.get_run("stale")
    assert run["status"] == "error" and run["approval"]["status"] == "expired"
