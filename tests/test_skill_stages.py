"""Tests for configurable pipeline-stage skill gating."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import api_server, skills_store


client = TestClient(api_server.app)


def _write_persona(root, slug, tag, keywords=()):
    persona_dir = root / slug
    persona_dir.mkdir(parents=True)
    triggers = "".join(f"  - {keyword}\n" for keyword in keywords)
    trigger_block = f"triggers:\n{triggers}" if triggers else ""
    metadata = f"metadata:\n  sdlc_stage: {tag}\n" if tag else ""
    (persona_dir / "SKILL.md").write_text(
        f"---\n"
        f"name: {slug}\n"
        f"description: {slug} persona\n"
        f"{trigger_block}"
        f"{metadata}"
        f"---\n"
        f"Ticket {{{{TICKET_KEY}}}} in {slug}\n",
        encoding="utf-8",
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    personas = tmp_path / "personas"
    _write_persona(personas, "backend-engineer", "implementation", ["endpoint"])
    _write_persona(personas, "qa-test-engineer", "verification", ["unit test"])
    _write_persona(personas, "security-reviewer", "verification", ["security"])
    _write_persona(personas, "solution-architect", "design", ["architecture"])
    monkeypatch.setattr(skills_store, "SKILLS_DIR", personas)
    monkeypatch.setattr(skills_store, "APP_SKILLS_DIR", tmp_path / "app-skills")
    monkeypatch.setattr(skills_store, "VERSIONS_DIR", tmp_path / "versions")
    stages_file = tmp_path / "stages.json"
    stages_file.write_text(
        json.dumps({
            "code": ["implementation"],
            "verify": ["verification"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_store, "STAGES_FILE", stages_file)
    return skills_store


def _slugs(entries):
    return [entry["slug"] for entry in entries]


def test_skills_are_filtered_by_pipeline_stage(store):
    assert _slugs(store.list_skills("code")) == ["backend-engineer"]
    assert _slugs(store.list_skills("verify")) == [
        "qa-test-engineer",
        "security-reviewer",
    ]
    assert len(store.list_skills()) == 4


def test_skills_report_which_pipeline_stages_can_load_them(store):
    by_slug = {entry["slug"]: entry["loads_in"] for entry in store.list_skills()}

    assert by_slug["backend-engineer"] == ["code"]
    assert by_slug["qa-test-engineer"] == ["verify"]
    assert by_slug["solution-architect"] == []


def test_persona_selection_only_considers_skills_allowed_in_that_stage(store):
    assert store.select_persona(
        "Add endpoint",
        "",
        stage="code",
    ) == "backend-engineer"
    assert store.select_persona(
        "Add endpoint",
        "",
        stage="verify",
    ) == "qa-test-engineer"
    assert store.select_persona(
        "security issue",
        "",
        stage="verify",
    ) == "security-reviewer"


def test_stage_scoped_fallback_uses_only_eligible_personas(store):
    assert store.select_persona(
        "Unmatched task",
        "",
        stage="code",
    ) == "backend-engineer"
    assert store.select_persona(
        "Unmatched task",
        "",
        stage="verify",
    ) == "qa-test-engineer"


def test_stage_scoped_rendering_rejects_a_skill_outside_the_pipeline_stage(store):
    with pytest.raises(skills_store.SkillValidationError, match="not allowed"):
        store.render_skill("qa-test-engineer", stage="code")


def test_stage_scoped_rendering_accepts_a_skill_allowed_in_the_pipeline_stage(store):
    rendered = store.render_skill(
        "backend-engineer",
        stage="code",
        TICKET_KEY="TEST-1",
    )

    assert "TEST-1" in rendered


def test_untagged_skills_do_not_bypass_configured_stage_gating(store):
    _write_persona(store.SKILLS_DIR, "general-helper", "")

    assert "general-helper" not in _slugs(store.list_skills("code"))
    assert "general-helper" not in _slugs(store.list_skills("verify"))
    assert next(
        entry for entry in store.list_skills() if entry["slug"] == "general-helper"
    )["loads_in"] == []


def test_stage_map_changes_eligibility_without_code_changes(store):
    store.STAGES_FILE.write_text(
        json.dumps({
            "code": ["implementation", "design"],
            "verify": ["verification"],
        }),
        encoding="utf-8",
    )

    assert _slugs(store.list_skills("code")) == [
        "backend-engineer",
        "solution-architect",
    ]
    assert store.persona_loads_in_stage("solution-architect", "code")
    assert not store.persona_loads_in_stage("qa-test-engineer", "code")


@pytest.mark.parametrize(
    "invalid_content",
    [
        "{ not json",
        json.dumps(["code", "verify"]),
        json.dumps({"code": "implementation"}),
        json.dumps({"code": ["unknown-tag"]}),
        "{}",
    ],
)
def test_invalid_stage_map_fails_clearly(store, invalid_content):
    store.STAGES_FILE.write_text(invalid_content, encoding="utf-8")

    with pytest.raises(skills_store.SkillValidationError):
        store.load_stage_map()


def test_unknown_pipeline_stage_is_rejected(store):
    with pytest.raises(
        skills_store.SkillValidationError,
        match="Unknown pipeline stage",
    ):
        store.list_skills("future")


def test_invalid_sdlc_stage_is_rejected_when_saving():
    content = (
        "---\n"
        "name: custom-persona\n"
        "description: A custom test persona.\n"
        "metadata:\n"
        "  sdlc_stage: testing\n"
        "triggers:\n"
        "  - test\n"
        "---\n"
        "Test persona body.\n"
    )

    with pytest.raises(skills_store.SkillValidationError, match="Invalid sdlc_stage"):
        skills_store._validate_content("custom-persona", content)


def test_shipped_stage_map_excludes_verification_personas_from_code():
    stage_map = skills_store.load_stage_map()

    assert stage_map == {
        "code": ["implementation"],
        "verify": ["verification"],
    }
    assert _slugs(skills_store.list_skills("code")) == [
        "backend-engineer",
        "frontend-engineer",
    ]
    assert _slugs(skills_store.list_skills("verify")) == [
        "qa-test-engineer",
        "security-reviewer",
        "verification-reviewer",
    ]
    assert skills_store.select_persona(
        "Unrelated implementation ticket",
        "",
        stage="verify",
    ) == "verification-reviewer"


def test_stage_map_defaults_are_returned_when_file_is_missing(tmp_path, monkeypatch):
    missing_stage_file = tmp_path / "missing.json"
    monkeypatch.setattr(skills_store, "STAGES_FILE", missing_stage_file)

    assert skills_store.load_stage_map() == skills_store.DEFAULT_STAGE_MAP


def test_app_skills_override_and_extend_shared_skills_without_leaking(store, tmp_path):
    shared_content = store.read_skill("backend-engineer")
    override_file = tmp_path / "override"
    _write_persona(
        override_file,
        "backend-engineer",
        "implementation",
        ["private endpoint"],
    )
    store.write_skill(
        "backend-engineer",
        (override_file / "backend-engineer" / "SKILL.md").read_text(
            encoding="utf-8",
        ),
        app_id="checkout",
    )

    additions_file = tmp_path / "addition"
    _write_persona(
        additions_file,
        "payments-specialist",
        "implementation",
        ["payments"],
    )
    store.create_skill(
        "payments-specialist",
        (additions_file / "payments-specialist" / "SKILL.md").read_text(
            encoding="utf-8",
        ),
        app_id="checkout",
    )

    checkout_skills = {
        skill["slug"]: skill
        for skill in store.list_skills(stage="code", app_id="checkout")
    }
    billing_skills = store.list_skills(stage="code", app_id="billing")
    shared_skills = store.list_skills(stage="code")

    assert checkout_skills["backend-engineer"]["source"] == "app"
    assert checkout_skills["backend-engineer"]["overrides_shared"] is True
    assert checkout_skills["payments-specialist"]["source"] == "app"
    assert "payments-specialist" not in _slugs(billing_skills)
    assert "payments-specialist" not in _slugs(shared_skills)
    assert store.read_skill("backend-engineer") == shared_content
    assert store.read_skill("backend-engineer", "billing") == shared_content
    assert "private endpoint" in store.read_skill("backend-engineer", "checkout")
    assert store.select_persona(
        "payments",
        "",
        stage="code",
        app_id="checkout",
    ) == "payments-specialist"
    assert "payments-specialist" in store.render_skill(
        "payments-specialist",
        stage="code",
        app_id="checkout",
    )


def test_app_skill_versions_are_scoped_to_the_application(store, tmp_path):
    override_file = tmp_path / "override"
    _write_persona(override_file, "backend-engineer", "implementation", ["private"])
    content = (override_file / "backend-engineer" / "SKILL.md").read_text(
        encoding="utf-8",
    )
    store.write_skill("backend-engineer", content, app_id="checkout")

    assert len(store.list_versions("backend-engineer", app_id="checkout")) == 1
    assert store.list_versions("backend-engineer", app_id="billing") == []
    assert store.list_versions("backend-engineer") == []


def test_shared_skill_version_can_be_restored_without_losing_current_version(
    store,
    tmp_path,
):
    original = store.read_skill("backend-engineer")
    changed_path = tmp_path / "changed"
    _write_persona(changed_path, "backend-engineer", "implementation", ["new"])
    changed = (
        changed_path / "backend-engineer" / "SKILL.md"
    ).read_text(encoding="utf-8")
    store.write_skill("backend-engineer", changed)

    versions = store.list_versions("backend-engineer")
    assert len(versions) == 1
    assert store.read_version(
        "backend-engineer",
        versions[0]["version_id"],
    ) == original

    store.restore_version("backend-engineer", versions[0]["version_id"])

    assert store.read_skill("backend-engineer") == original
    retained_versions = store.list_versions("backend-engineer")
    assert len(retained_versions) == 2
    assert changed in {
        store.read_version("backend-engineer", version["version_id"])
        for version in retained_versions
    }


def test_stage_mapping_and_filtered_skills_are_exposed_by_the_api():
    stages_response = client.get("/api/stages")
    skills_response = client.get("/api/skills?stage=verify")

    assert stages_response.status_code == 200
    assert stages_response.json() == {
        "code": ["implementation"],
        "verify": ["verification"],
    }
    assert skills_response.status_code == 200
    assert _slugs(skills_response.json()) == [
        "qa-test-engineer",
        "security-reviewer",
        "verification-reviewer",
    ]


def test_public_marketplace_skill_can_be_previewed_and_imported_per_app(
    store,
    tmp_path,
    monkeypatch,
):
    cache_dir = tmp_path / "sdk-cache"
    source = cache_dir / "extensions" / "skills" / "public-helper" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source_content = (
        "---\n"
        "name: public-helper\n"
        "description: Public helper instructions.\n"
        "license: MIT\n"
        "---\n"
        "Public helper body.\n"
    )
    source.write_text(source_content, encoding="utf-8")
    public_skill = skills_store.Skill.load(source)
    monkeypatch.setattr(skills_store, "get_skills_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(
        skills_store,
        "load_public_skills",
        lambda: [public_skill],
    )
    monkeypatch.setattr(skills_store, "SKILLS_DIR", tmp_path / "shared")
    monkeypatch.setattr(skills_store, "APP_SKILLS_DIR", tmp_path / "app-skills")

    catalog = client.get("/api/skills/marketplace?app_id=default")
    preview = client.get("/api/skills/marketplace/public-helper")
    install = client.post(
        "/api/skills/marketplace/public-helper/install",
        json={"app_id": "default", "stage": "implementation"},
    )

    assert catalog.status_code == 200, catalog.text
    assert catalog.json() == [{
        "slug": "public-helper",
        "name": "public-helper",
        "description": "Public helper instructions.",
        "license": "MIT",
        "is_installed": False,
    }]
    assert preview.status_code == 200
    assert preview.json()["content"] == source_content
    assert install.status_code == 200, install.text

    imported = store.read_skill("public-helper", app_id="default")
    assert "sdlc_stage: implementation" in imported
    assert "Public helper body." in imported
    assert not (tmp_path / "shared" / "public-helper" / "SKILL.md").exists()


@pytest.mark.usefixtures("store")
def test_marketplace_import_requires_a_stage_enabled_by_the_pipeline(
    tmp_path,
    monkeypatch,
):
    cache_dir = tmp_path / "sdk-cache"
    source = cache_dir / "extensions" / "skills" / "public-helper" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: public-helper\ndescription: Helper.\n---\nBody.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_store, "get_skills_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(
        skills_store,
        "load_public_skills",
        lambda: [skills_store.Skill.load(source)],
    )

    with pytest.raises(
        skills_store.SkillValidationError,
        match="not enabled",
    ):
        skills_store.install_public_skill(
            "public-helper",
            "design",
            "checkout",
        )


def test_public_marketplace_catalog_rejects_sources_outside_sdk_cache(
    tmp_path,
    monkeypatch,
):
    cache_dir = tmp_path / "sdk-cache"
    source = tmp_path / "outside" / "public-helper" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: public-helper\ndescription: Helper.\n---\nBody.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_store, "get_skills_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(
        skills_store,
        "load_public_skills",
        lambda: [skills_store.Skill.load(source)],
    )

    with pytest.raises(skills_store.SkillMarketplaceUnavailable):
        skills_store.list_public_skills()


def test_marketplace_import_does_not_overwrite_a_shared_skill(store):
    shared_path = store.SKILLS_DIR / "public-helper" / "SKILL.md"
    shared_path.parent.mkdir()
    shared_path.write_text(
        "---\n"
        "name: public-helper\n"
        "description: Existing shared persona.\n"
        "metadata:\n"
        "  sdlc_stage: implementation\n"
        "---\n"
        "Keep this shared content.\n",
        encoding="utf-8",
    )

    with pytest.raises(FileExistsError, match="already exists"):
        store.install_public_skill(
            "public-helper",
            "implementation",
            "checkout",
        )

    assert "Keep this shared content." in shared_path.read_text(encoding="utf-8")
