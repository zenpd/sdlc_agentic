"""Tests for stack modifiers (skills_store.py): detect the repo's language and
mix in the matching notes. Uses the real skills/modifiers/ files shipped with
the project, plus fake repos built in a temp folder."""

from backend import skills_store


def _touch(root, relative_path):
    f = root / relative_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("", encoding="utf-8")


def test_all_shipped_modifiers_load_and_declare_markers():
    entries = skills_store.list_modifiers()
    assert {e["slug"] for e in entries} >= {"python", "typescript", "go", "java"}
    for e in entries:
        assert "error" not in e, e
        assert e["detect_files"], f"{e['slug']} has no detect_files"


def test_detects_go_from_go_mod(tmp_path):
    _touch(tmp_path, "go.mod")
    assert skills_store.detect_modifiers(tmp_path) == ["go"]


def test_detects_python_from_pyproject(tmp_path):
    _touch(tmp_path, "pyproject.toml")
    assert skills_store.detect_modifiers(tmp_path) == ["python"]


def test_empty_repo_gets_no_modifiers(tmp_path):
    _touch(tmp_path, "README.md")
    assert skills_store.detect_modifiers(tmp_path) == []


def test_monorepo_subfolders_count_and_modifiers_compose(tmp_path):
    _touch(tmp_path, "backend/pyproject.toml")   # one folder down
    _touch(tmp_path, "ui/tsconfig.json")         # one folder down
    assert skills_store.detect_modifiers(tmp_path) == [
        "python",
        "typescript",
    ]  # alphabetical


def test_detects_markers_after_many_unrelated_subfolders(tmp_path):
    for index in range(51):
        (tmp_path / f"folder-{index:02}").mkdir()
    _touch(tmp_path, "folder-50/go.mod")

    assert skills_store.detect_modifiers(tmp_path) == ["go"]


def test_marker_directory_does_not_count_as_a_stack_file(tmp_path):
    (tmp_path / "go.mod").mkdir()

    assert skills_store.detect_modifiers(tmp_path) == []


def test_ignores_dependency_folders(tmp_path):
    _touch(tmp_path, "node_modules/some-pkg/tsconfig.json")
    _touch(tmp_path, ".venv/pyproject.toml")
    assert skills_store.detect_modifiers(tmp_path) == []


def test_never_returns_more_than_max_modifiers(tmp_path, monkeypatch):
    for marker in ["go.mod", "pyproject.toml", "tsconfig.json", "pom.xml"]:
        _touch(tmp_path, marker)
    monkeypatch.setattr(skills_store, "MAX_MODIFIERS", 2)
    assert skills_store.detect_modifiers(tmp_path) == [
        "go",
        "java",
    ]  # first two alphabetically


def test_render_modifiers_has_one_section_per_stack_and_handles_empty():
    text = skills_store.render_modifiers(["go", "python"], TICKET_KEY="X-1")
    assert "[go]" in text and "[python]" in text
    assert text.index("[go]") < text.index("[python]")
    assert skills_store.render_modifiers([]) == ""


def test_render_modifiers_warns_and_skips_unknown_slug(caplog):
    text = skills_store.render_modifiers(["does-not-exist", "go"])
    assert "[go]" in text and "does-not-exist" not in text
    assert "could not be loaded" in caplog.text


def test_a_broken_modifier_is_never_applied(tmp_path, monkeypatch):
    bad = tmp_path / "modifiers" / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter at all", encoding="utf-8")
    monkeypatch.setattr(skills_store, "MODIFIERS_DIR", tmp_path / "modifiers")

    repo = tmp_path / "repo"
    _touch(repo, "go.mod")
    assert skills_store.detect_modifiers(repo) == []


def test_modifier_marker_must_be_a_file_name(tmp_path, monkeypatch):
    modifier = tmp_path / "modifiers" / "unsafe"
    modifier.mkdir(parents=True)
    (modifier / "SKILL.md").write_text(
        "---\n"
        "name: unsafe\n"
        "description: Invalid marker path.\n"
        "metadata:\n"
        '  detect_files: "../go.mod"\n'
        "---\n"
        "Do not apply.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_store, "MODIFIERS_DIR", tmp_path / "modifiers")

    entries = skills_store.list_modifiers()
    assert entries[0]["slug"] == "unsafe"
    assert "error" in entries[0]