"""Suggested verification and governance settings for an application, read
from the file list of its target repository (no file contents needed).

Used by the Config page to pre-fill the test command, CI workflow and
protected paths when someone picks a verification mode or turns on approval.
"""

import posixpath

import requests

# Fallback when the repository can't be read, or its stack isn't recognised.
GENERIC = {"stack": None, "test_command": "", "setup_command": ""}
DEFAULT_PROTECTED = [".github/workflows/*"]

# (stack name, marker files at the repo root, setup command, test command) —
# the first stack whose marker is present wins.
_STACKS = [
    ("Node.js (pnpm)", ("pnpm-lock.yaml",), "pnpm install --frozen-lockfile", "pnpm test"),
    ("Node.js (yarn)", ("yarn.lock",), "yarn install --frozen-lockfile", "yarn test"),
    ("Node.js", ("package-lock.json",), "npm ci", "npm test"),
    ("Node.js", ("package.json",), "npm install", "npm test"),
    ("Python", ("requirements.txt",), "pip install -r requirements.txt", "python -m pytest -q"),
    ("Python", ("pyproject.toml", "setup.py", "pytest.ini", "tox.ini"), "pip install -e .", "python -m pytest -q"),
    ("Go", ("go.mod",), "go mod download", "go test ./..."),
    ("Java (Maven)", ("pom.xml",), "", "mvn -q test"),
    ("Java (Gradle)", ("gradlew",), "", "./gradlew test"),
    ("Java (Gradle)", ("build.gradle", "build.gradle.kts"), "", "gradle test"),
    ("Rust", ("Cargo.toml",), "", "cargo test"),
    (".NET", (".sln", ".csproj"), "", "dotnet test"),
]

# Folders that hold infrastructure or deployment config — off-limits to the agent.
_INFRA_DIRS = ("infra", "terraform", "deploy", "deployment", "k8s", "kubernetes", "helm", "charts", "ops")


def suggest_from_paths(paths: list[str]) -> dict:
    """Pure function: repository file paths -> suggested settings."""
    root = {p for p in paths if "/" not in p}
    root_exts = {posixpath.splitext(p)[1] for p in root}

    result = dict(GENERIC)
    for stack, markers, setup, test in _STACKS:
        if any(m in root or (m.startswith(".") and m in root_exts) for m in markers):
            result = {"stack": stack, "test_command": test, "setup_command": setup}
            break

    workflows = sorted(
        posixpath.basename(p) for p in paths
        if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml")) and p.count("/") == 2
    )

    protected = []
    if workflows or any(p.startswith(".github/workflows/") for p in paths):
        protected.append(".github/workflows/*")
    top_dirs = {p.split("/", 1)[0] for p in paths if "/" in p}
    protected += [f"{d}/*" for d in _INFRA_DIRS if d in top_dirs]
    if any(p.endswith(".tf") for p in paths):
        protected.append("*.tf")
    if "Dockerfile" in root:
        protected.append("Dockerfile")

    return {
        **result,
        "ci_workflows": workflows,
        "protected_paths": protected or list(DEFAULT_PROTECTED),
    }


def suggest_for_repo(token: str, repo: str, branch: str) -> dict:
    """Read the repo's file list from GitHub and suggest settings. Never raises:
    an unreadable repo gives the generic suggestion plus the reason."""
    out: dict = {"repo": repo, "branch": branch, "source": "generic", "reason": None}
    if not (token and repo):
        return {**out, **suggest_from_paths([]), "reason": "No repository or GitHub token set for this application"}
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/git/trees/{branch or 'HEAD'}",
            params={"recursive": "1"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
    except requests.RequestException as e:
        return {**out, **suggest_from_paths([]), "reason": f"Couldn't reach GitHub ({type(e).__name__})"}
    if resp.status_code != 200:
        reason = {401: "GitHub token expired — re-select the repo on the Applications page",
                  404: f"Repository {repo}@{branch} not found"}.get(resp.status_code, f"GitHub returned {resp.status_code}")
        return {**out, **suggest_from_paths([]), "reason": reason}
    try:
        tree = resp.json().get("tree", [])
    except ValueError:
        return {**out, **suggest_from_paths([]), "reason": "GitHub returned an unexpected response"}
    paths = [t["path"] for t in tree if t.get("type") == "blob" and isinstance(t.get("path"), str)]
    suggestion = suggest_from_paths(paths)
    return {**out, **suggestion, "source": "repo" if suggestion["stack"] or paths else "generic"}
