"""Config page pre-fill: suggested test command / workflows / protected paths
detected from the application's repository file list."""

from types import SimpleNamespace

import pytest

from conftest import client, sign_in
from backend import apps_store, repo_suggest, users_store
from backend.repo_suggest import suggest_from_paths


def test_python_repo_gets_pytest_and_its_workflows_protected():
    s = suggest_from_paths(["requirements.txt", "pytest.ini", "src/app.py",
                            ".github/workflows/ci.yml", ".github/workflows/deploy.yaml", ".github/images/x.png"])
    assert (s["stack"], s["test_command"], s["setup_command"]) == ("Python", "python -m pytest -q", "pip install -r requirements.txt")
    assert s["ci_workflows"] == ["ci.yml", "deploy.yaml"]
    assert s["protected_paths"] == [".github/workflows/*"]


@pytest.mark.parametrize("paths, stack, test", [
    (["package.json", "package-lock.json"], "Node.js", "npm test"),
    (["package.json", "yarn.lock"], "Node.js (yarn)", "yarn test"),
    (["go.mod", "main.go"], "Go", "go test ./..."),
    (["pom.xml"], "Java (Maven)", "mvn -q test"),
    (["App.sln", "src/App/App.csproj"], ".NET", "dotnet test"),
    (["pyproject.toml"], "Python", "python -m pytest -q"),
])
def test_stack_detection(paths, stack, test):
    s = suggest_from_paths(paths)
    assert (s["stack"], s["test_command"]) == (stack, test)


def test_infra_and_docker_are_protected_and_unknown_stack_stays_blank():
    s = suggest_from_paths(["README.md", "Dockerfile", "infra/main.tf", "k8s/deploy.yaml", "docs/x.md"])
    assert s["stack"] is None and s["test_command"] == ""
    assert s["protected_paths"] == ["infra/*", "k8s/*", "*.tf", "Dockerfile"]


def test_nothing_to_protect_falls_back_to_workflows():
    assert suggest_from_paths(["README.md"])["protected_paths"] == [".github/workflows/*"]


def test_endpoint_reads_the_apps_repo(monkeypatch):
    users_store.ensure_user("first-admin")
    sign_in("priya", "maintainer")
    app = apps_store.create_app("Checkout", config={"TARGET_REPO": "acme/checkout", "TARGET_BRANCH": "main", "GITHUB_TOKEN": "gho_x"})
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, auth=headers["Authorization"])
        return SimpleNamespace(status_code=200, json=lambda: {"tree": [
            {"path": "go.mod", "type": "blob"}, {"path": ".github/workflows/test.yml", "type": "blob"},
        ]})

    monkeypatch.setattr(repo_suggest.requests, "get", fake_get)
    r = client.get(f"/api/apps/{app['id']}/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert seen == {"url": "https://api.github.com/repos/acme/checkout/git/trees/main", "auth": "Bearer gho_x"}
    assert (body["source"], body["stack"], body["test_command"], body["ci_workflows"]) == ("repo", "Go", "go test ./...", ["test.yml"])


def test_endpoint_explains_an_expired_token_and_needs_maintainer(monkeypatch):
    monkeypatch.setattr(repo_suggest.requests, "get", lambda *a, **k: SimpleNamespace(status_code=401))
    users_store.ensure_user("first-admin")
    sign_in("rahul", "runner")
    assert client.get("/api/apps/default/suggestions").status_code == 403
    sign_in("priya", "maintainer")
    body = client.get("/api/apps/default/suggestions").json()
    assert body["source"] == "generic" and "expired" in body["reason"]
    assert body["test_command"] == "" and body["protected_paths"] == [".github/workflows/*"]
