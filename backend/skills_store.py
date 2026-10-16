"""Persona skill discovery, selection, and rendering.

Shared skills live in skills/personas; app-specific skills live in
skills/apps/<app_id>/personas and override shared skills with the same slug.
"""

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import frontmatter

from openhands.sdk.skills import Skill, load_public_skills
from openhands.sdk.skills.trigger import KeywordTrigger
from openhands.sdk.skills.utils import get_skills_cache_dir


SKILLS_DIR = Path(__file__).parent.parent / "skills" / "personas"
APP_SKILLS_DIR = SKILLS_DIR.parent / "apps"
DEFAULT_PERSONA = "solution-architect"
DEFAULT_PERSONAS_BY_STAGE = {"verify": "verification-reviewer"}
logger = logging.getLogger(__name__)
SDLC_STAGES = frozenset({
    "discovery",
    "design",
    "implementation",
    "verification",
    "release",
    "operate",
})
STAGES_FILE = SKILLS_DIR.parent / "stages.json"
DEFAULT_STAGE_MAP = {
    "code": ["implementation"],
    "verify": ["verification"],
}

# Skill versioning: every time an existing skill is overwritten, its previous
# content is copied to skills/.versions/<slug>/<timestamp>.md first.  The
# backups deliberately live OUTSIDE personas/ so list_skills() and
# select_persona() can never mistake a backup for a real persona.
VERSIONS_DIR = SKILLS_DIR.parent / ".versions"
MAX_VERSIONS = 20  # kept per skill; the oldest are deleted beyond this

# Stack modifiers: small add-on skills ("python", "go", ...) that are mixed in
# on top of a persona when the cloned repo uses that language. They live next
# to personas/, in skills/modifiers/<slug>/SKILL.md, and say how to recognise
# their stack through `metadata.detect_files` in their frontmatter - so adding
# a new language is just adding a folder, with no Python change.
MODIFIERS_DIR = SKILLS_DIR.parent / "modifiers"
MAX_MODIFIERS = 3  # cap so a huge monorepo can't bloat the agent's prompt
# Folders never worth looking inside when sniffing a repo's language
_SKIP_DIRS = {
    "node_modules",
    "venv",
    ".venv",
    "env",
    "vendor",
    "dist",
    "build",
    "target",
    "__pycache__",
}

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_VERSION_FMT = "%Y%m%dT%H%M%S%fZ"  # e.g. 20261013T132155123456Z (UTC)
_VERSION_ID_RE = re.compile(r"^\d{8}T\d{12}Z$")

# The {{VAR}} contract documented in skills/README.md — kept here as the
# single source of truth for what render_skill() accepts.
VARIABLE_NAMES = ["TICKET_KEY", "TICKET_SUMMARY", "TARGET_REPO", "TARGET_BRANCH", "PERSONA"]


class SkillValidationError(Exception):
    """Raised when a skill's content fails to parse as a valid SKILL.md."""


class SkillMarketplaceUnavailable(Exception):
    """Raised when the SDK cannot provide its public skill catalog."""


def _check_slug(slug: str) -> None:
    """Reject slugs that could escape their skill directory."""
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise SkillValidationError(f"Invalid skill name {slug!r}.")


def _check_app_id(app_id: str) -> None:
    if not isinstance(app_id, str) or not _SLUG_RE.fullmatch(app_id):
        raise SkillValidationError(f"Invalid application id {app_id!r}.")


def _app_personas_dir(app_id: str) -> Path:
    _check_app_id(app_id)
    return APP_SKILLS_DIR / app_id / "personas"


def _app_skill_path(slug: str, app_id: str) -> Path:
    _check_slug(slug)
    return _app_personas_dir(app_id) / slug / "SKILL.md"


def _skill_path(slug: str, app_id: str | None = None) -> Path:
    """Resolve the app override when present, otherwise use the shared skill."""
    _check_slug(slug)
    if app_id is not None:
        app_path = _app_skill_path(slug, app_id)
        if app_path.exists():
            return app_path
    return SKILLS_DIR / slug / "SKILL.md"


def _version_dir(slug: str, app_id: str | None = None) -> Path:
    _check_slug(slug)
    if app_id is None:
        return VERSIONS_DIR / slug
    _check_app_id(app_id)
    return VERSIONS_DIR / "apps" / app_id / slug


def delete_app_skills(app_id: str) -> None:
    """Remove only the app-owned skill and version directories."""
    _check_app_id(app_id)
    for path in (
        APP_SKILLS_DIR / app_id,
        VERSIONS_DIR / "apps" / app_id,
    ):
        if path.exists():
            shutil.rmtree(path)


def _keywords_of(skill: Skill) -> list[str]:
    return skill.trigger.keywords if isinstance(skill.trigger, KeywordTrigger) else []


def _sdlc_stage_of(skill: Skill) -> str:
    """Return a skill's stage, rejecting labels that cannot be used to gate it."""
    stage = (skill.metadata or {}).get("sdlc_stage", "")
    if not stage:
        return ""
    if not isinstance(stage, str) or stage not in SDLC_STAGES:
        raise SkillValidationError(
            f"Invalid sdlc_stage {stage!r}; expected one of "
            f"{', '.join(sorted(SDLC_STAGES))}."
        )
    return stage


def load_stage_map() -> dict[str, list[str]]:
    """Load pipeline-stage to skill-tag mappings from skills/stages.json."""
    try:
        raw = json.loads(STAGES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {stage: list(tags) for stage, tags in DEFAULT_STAGE_MAP.items()}
    except (OSError, json.JSONDecodeError) as e:
        raise SkillValidationError(f"Could not read {STAGES_FILE}: {e}") from e

    if not isinstance(raw, dict) or not raw:
        raise SkillValidationError(
            "skills/stages.json must be a non-empty object mapping pipeline "
            "stages to lists of SDLC skill tags."
        )

    stage_map: dict[str, list[str]] = {}
    for pipeline_stage, skill_tags in raw.items():
        if (
            not isinstance(pipeline_stage, str)
            or not _SLUG_RE.fullmatch(pipeline_stage)
            or not isinstance(skill_tags, list)
            or any(
                not isinstance(tag, str) or tag not in SDLC_STAGES
                for tag in skill_tags
            )
        ):
            raise SkillValidationError(
                "Each stages.json entry must have a valid pipeline-stage name "
                "and a list of supported SDLC skill tags."
            )
        stage_map[pipeline_stage] = skill_tags
    return stage_map


def _validate_stage(stage: str, stage_map: dict[str, list[str]]) -> None:
    if not isinstance(stage, str) or stage not in stage_map:
        raise SkillValidationError(
            f"Unknown pipeline stage {stage!r}; configured stages are "
            f"{', '.join(stage_map)}."
        )


def _tag_allowed(
    tag: str,
    pipeline_stage: str,
    stage_map: dict[str, list[str]],
) -> bool:
    return bool(tag) and tag in stage_map[pipeline_stage]


def _loads_in(tag: str, stage_map: dict[str, list[str]]) -> list[str]:
    return [
        pipeline_stage
        for pipeline_stage in stage_map
        if _tag_allowed(tag, pipeline_stage, stage_map)
    ]


def list_skills(
    stage: str | None = None,
    app_id: str | None = None,
) -> list[dict]:
    """List all persona skills with their parsed frontmatter metadata.

    A skill whose file fails to parse is still listed (with an "error" key)
    rather than silently dropped, so a bad edit is visible in the UI instead
    of just disappearing from the list. When `stage` is supplied, only skills
    allowed by that pipeline stage are returned. Each valid skill also lists
    its eligible pipeline stages in `loads_in`.
    """
    stage_map = load_stage_map()
    if stage is not None:
        _validate_stage(stage, stage_map)
    shared = {
        d.name: d / "SKILL.md"
        for d in SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    } if SKILLS_DIR.exists() else {}
    app_personas_dir = _app_personas_dir(app_id) if app_id is not None else None
    skills = dict(shared)
    if app_personas_dir is not None and app_personas_dir.exists():
        skills.update({
            d.name: d / "SKILL.md"
            for d in app_personas_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        })

    out = []
    for slug, f in sorted(skills.items()):
        source = (
            "app"
            if app_personas_dir is not None and f.is_relative_to(app_personas_dir)
            else "shared"
        )
        try:
            skill = Skill.load(f)
            sdlc_stage = _sdlc_stage_of(skill)
        except Exception as e:
            out.append({
                "slug": slug,
                "source": source,
                "overrides_shared": source == "app" and slug in shared,
                "loads_in": [],
                "error": str(e),
            })
            continue
        if stage is not None and not _tag_allowed(sdlc_stage, stage, stage_map):
            continue
        out.append({
            "slug": slug,
            "name": skill.name,
            "description": skill.description or "",
            "sdlc_stage": sdlc_stage,
            "keywords": _keywords_of(skill),
            "loads_in": _loads_in(sdlc_stage, stage_map),
            "source": source,
            "overrides_shared": source == "app" and slug in shared,
        })
    return out


def read_skill(slug: str, app_id: str | None = None) -> str:
    """Raw file content (frontmatter + body) for editing."""
    path = _skill_path(slug, app_id)
    if not path.exists():
        raise FileNotFoundError(f"No skill named {slug!r}")
    return path.read_text(encoding="utf-8")


def _validate_content(slug: str, content: str) -> Skill:
    """Parse `content` as if it were `skills/personas/<slug>/SKILL.md`,
    without touching the real file — raises SkillValidationError on any
    parse/naming problem (mirrors the directory-name-must-match-frontmatter
    rule the SDK enforces).

    Skill.load() is deliberately lenient (frontmatter is optional in the
    general markdown sense, so content with none just becomes a nameless
    skill using the directory name) — that's too permissive for a save
    coming through this app's own edit form, so this adds the stricter
    checks a *persona* skill specifically needs: real frontmatter, and a
    non-empty description.
    """
    if not content.strip().startswith("---"):
        raise SkillValidationError(
            "Missing YAML frontmatter — a skill file must start with a "
            "'---' delimited block (name, description, ...)."
        )
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / slug
        skill_dir.mkdir()
        tmp_file = skill_dir / "SKILL.md"
        tmp_file.write_text(content, encoding="utf-8")
        try:
            skill = Skill.load(tmp_file)
        except Exception as e:
            raise SkillValidationError(str(e)) from e
    if not skill.description or not skill.description.strip():
        raise SkillValidationError(
            "Frontmatter is missing a non-empty 'description' field."
        )
    if not skill.content.strip():
        raise SkillValidationError("Skill body (below the frontmatter) is empty.")
    _sdlc_stage_of(skill)
    return skill


def _norm(text: str) -> str:
    """Normalise Windows/Unix line endings so 'same text' compares equal."""
    return text.replace("\r\n", "\n")


def _snapshot(
    slug: str,
    old_content: str,
    app_id: str | None = None,
) -> None:
    """Save `old_content` as a new version of `slug`, then prune old ones."""
    vdir = _version_dir(slug, app_id)
    vdir.mkdir(parents=True, exist_ok=True)

    # Timestamp (UTC, microseconds) is the version id.  If two saves land in
    # the same instant, nudge forward 1µs so we never overwrite a version.
    now = datetime.now(timezone.utc)
    while (vdir / f"{now.strftime(_VERSION_FMT)}.md").exists():
        now += timedelta(microseconds=1)
    (vdir / f"{now.strftime(_VERSION_FMT)}.md").write_text(old_content, encoding="utf-8")

    # File names sort in time order, so everything except the newest
    # MAX_VERSIONS files is the oldest stuff — delete it.
    files = sorted(vdir.glob("*.md"))
    for stale in files[:-MAX_VERSIONS]:
        stale.unlink()


def write_skill(
    slug: str,
    content: str,
    app_id: str | None = None,
) -> None:
    """Validate then persist a skill's content. Never writes a file that
    fails to parse — an existing skill is left untouched on a bad save.

    If the skill already exists and the text actually changed, its previous
    content is saved as a version first, so a bad edit can be undone."""
    _check_slug(slug)
    if app_id is not None:
        _check_app_id(app_id)
    _validate_content(slug, content)
    path = _app_skill_path(slug, app_id) if app_id is not None else _skill_path(slug)
    current_path = _skill_path(slug, app_id)
    old_content = (
        current_path.read_text(encoding="utf-8") if current_path.exists() else None
    )
    if old_content is not None and _norm(old_content) == _norm(content):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if old_content is not None:
        _snapshot(slug, old_content, app_id)
    path.write_text(content, encoding="utf-8")


def list_versions(slug: str, app_id: str | None = None) -> list[dict]:
    """Earlier versions of a skill, newest first. Empty list if none yet."""
    vdir = _version_dir(slug, app_id)
    if not vdir.exists():
        return []
    out = []
    for f in sorted(vdir.glob("*.md"), reverse=True):
        if not _VERSION_ID_RE.match(f.stem):
            continue  # ignore stray files someone dropped in the folder
        saved_at = datetime.strptime(f.stem, _VERSION_FMT).replace(tzinfo=timezone.utc)
        out.append({
            "version_id": f.stem,
            "saved_at": saved_at.isoformat(),
            "size_bytes": f.stat().st_size,
        })
    return out


def read_version(
    slug: str,
    version_id: str,
    app_id: str | None = None,
) -> str:
    """Raw content of one earlier version."""
    if not _VERSION_ID_RE.match(version_id):
        raise SkillValidationError(f"Invalid version id {version_id!r}.")
    path = _version_dir(slug, app_id) / f"{version_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"No version {version_id!r} of skill {slug!r}")
    return path.read_text(encoding="utf-8")


def restore_version(
    slug: str,
    version_id: str,
    app_id: str | None = None,
) -> None:
    """Make an earlier version the live skill again.

    Goes through write_skill(), so the content is validated again and the
    version being replaced is itself saved first — a restore can be undone."""
    write_skill(slug, read_version(slug, version_id, app_id), app_id)


def create_skill(
    slug: str,
    content: str,
    app_id: str | None = None,
) -> None:
    """Create a new skill or app-local override without touching shared files."""
    path = _app_skill_path(slug, app_id) if app_id is not None else _skill_path(slug)
    if path.exists():
        raise FileExistsError(f"Skill {slug!r} already exists")
    write_skill(slug, content, app_id)


def _public_marketplace_skills() -> list[tuple[Skill, Path]]:
    """Return installable AgentSkills-format entries from the SDK catalog."""
    skills = load_public_skills()
    cache_dir = Path(get_skills_cache_dir()).resolve()
    catalog: list[tuple[Skill, Path]] = []
    for skill in skills:
        if not skill.is_agentskills_format or not _SLUG_RE.fullmatch(skill.name):
            continue
        if not skill.source:
            continue
        source = Path(skill.source)
        try:
            source = source.resolve(strict=True)
        except OSError:
            continue
        if (
            source.name.lower() != "skill.md"
            or not source.is_file()
            or not source.is_relative_to(cache_dir)
        ):
            continue
        catalog.append((skill, source))
    if not catalog:
        raise SkillMarketplaceUnavailable(
            "The OpenHands public skill catalog is unavailable or contains no "
            "installable AgentSkills entries."
        )
    return sorted(catalog, key=lambda item: item[0].name.casefold())


def list_public_skills(app_id: str | None = None) -> list[dict]:
    """List installable skills from the SDK's public OpenHands marketplace."""
    if app_id is not None:
        _check_app_id(app_id)
    installed_names = {
        entry["slug"] for entry in list_skills(app_id=app_id)
    }
    return [
        {
            "slug": skill.name,
            "name": skill.name,
            "description": skill.description or "",
            "license": skill.license or "",
            "is_installed": skill.name in installed_names,
        }
        for skill, _ in _public_marketplace_skills()
    ]


def read_public_skill(slug: str) -> str:
    """Read a marketplace skill's source markdown for a safe preview."""
    _check_slug(slug)
    for skill, source in _public_marketplace_skills():
        if skill.name == slug:
            return source.read_text(encoding="utf-8")
    raise FileNotFoundError(f"No public marketplace skill named {slug!r}")


def install_public_skill(
    slug: str,
    stage: str,
    app_id: str,
) -> None:
    """Import a public skill's markdown into one application's persona library."""
    _check_slug(slug)
    _check_app_id(app_id)
    stage_map = load_stage_map()
    if stage not in {tag for tags in stage_map.values() for tag in tags}:
        raise SkillValidationError(
            f"SDLC stage {stage!r} is not enabled by any configured pipeline stage."
        )

    app_path = _app_skill_path(slug, app_id)
    shared_path = SKILLS_DIR / slug / "SKILL.md"
    if app_path.exists() or shared_path.exists():
        raise FileExistsError(
            f"Skill {slug!r} already exists in this application or the shared library."
        )

    source_content = read_public_skill(slug)
    post = frontmatter.loads(source_content)
    metadata = post.metadata.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise SkillValidationError(
            "Marketplace skill metadata must be a YAML mapping to assign an SDLC stage."
        )
    metadata["sdlc_stage"] = stage
    post.metadata["metadata"] = metadata
    create_skill(slug, frontmatter.dumps(post), app_id)


def select_persona(
    summary: str,
    description: str,
    stage: str | None = None,
    app_id: str | None = None,
) -> str:
    """Score a ticket's text against each skill's trigger keywords, return
    the best-matching persona slug. If `stage` is supplied, only skills tagged
    for that stage can be selected. A stage-scoped selection falls back to the
    default persona when eligible, then the first eligible slug alphabetically.

    The summary counts far more than the description: a ticket's title is a
    concise, deliberate signal of what it's fundamentally about, while the
    description is long-form prose that naturally accumulates generic
    boilerplate — "add unit tests covering X" in an implementation ticket's
    acceptance criteria shouldn't be enough to route it to QA over the
    persona whose keywords actually match the title.
    """
    summary_l = summary.lower()
    description_l = description.lower()
    SUMMARY_WEIGHT = 3
    DESCRIPTION_WEIGHT = 1

    best_slug: Optional[str] = None
    best_score = 0
    entries = list_skills(stage=stage, app_id=app_id)
    for entry in entries:
        if "error" in entry:
            continue
        score = 0
        for kw in entry["keywords"]:
            kw_l = kw.lower()
            if kw_l in summary_l:
                score += SUMMARY_WEIGHT
            if kw_l in description_l:
                score += DESCRIPTION_WEIGHT
        if score > best_score:
            best_score = score
            best_slug = entry["slug"]
    if best_slug:
        return best_slug
    if stage is None:
        return DEFAULT_PERSONA
    if not entries:
        raise SkillValidationError(
            f"No persona skills are configured for {stage!r}."
        )
    eligible_slugs = {entry["slug"] for entry in entries if "error" not in entry}
    if not eligible_slugs:
        raise SkillValidationError(f"No valid persona skills are configured for {stage!r}.")
    stage_default = DEFAULT_PERSONAS_BY_STAGE.get(stage, DEFAULT_PERSONA)
    if stage_default in eligible_slugs:
        return stage_default
    return min(eligible_slugs)


def persona_loads_in_stage(
    slug: str,
    stage: str,
    app_id: str | None = None,
) -> bool:
    """Return whether a persona can load in a configured pipeline stage."""
    entries = list_skills(stage=stage, app_id=app_id)
    return any(entry["slug"] == slug and "error" not in entry for entry in entries)


def render_skill(
    slug: str,
    *,
    stage: str | None = None,
    app_id: str | None = None,
    **variables: str,
) -> str:
    """Load and render a skill, optionally enforcing pipeline-stage eligibility."""
    skill = Skill.load(_skill_path(slug, app_id))
    if stage is not None:
        stage_map = load_stage_map()
        _validate_stage(stage, stage_map)
        skill_stage = _sdlc_stage_of(skill)
        if not _tag_allowed(skill_stage, stage, stage_map):
            raise SkillValidationError(
                f"Skill {slug!r} with SDLC stage "
                f"{skill_stage or 'no stage'} is not allowed in pipeline "
                f"stage {stage!r}."
            )
    content = skill.content
    for name, value in variables.items():
        content = content.replace(f"{{{{{name}}}}}", value)
    return content


# ── Stack modifiers (persona + language/framework, composed) ─────────
def _modifier_path(slug: str) -> Path:
    return MODIFIERS_DIR / slug / "SKILL.md"


def _detect_files_of(skill: Skill) -> list[str]:
    """File names that signal this stack, from `metadata.detect_files`
    (a comma-separated string, e.g. "go.mod,go.work")."""
    raw = (skill.metadata or {}).get("detect_files", "")
    if not isinstance(raw, str):
        raise SkillValidationError(
            "metadata.detect_files must be a comma-separated string."
        )

    markers = [name.strip() for name in raw.split(",") if name.strip()]
    if not markers:
        raise SkillValidationError(
            "metadata.detect_files must include at least one file name."
        )
    if any(Path(name).name != name or name in {".", ".."} for name in markers):
        raise SkillValidationError(
            "metadata.detect_files entries must be file names, not paths."
        )
    return markers


def list_modifiers() -> list[dict]:
    """All stack modifiers with their parsed metadata. Like list_skills(),
    a file that fails to parse is listed with an "error" key instead of being
    silently dropped."""
    out = []
    if not MODIFIERS_DIR.exists():
        return out
    for d in sorted(p for p in MODIFIERS_DIR.iterdir() if p.is_dir()):
        f = d / "SKILL.md"
        if not f.exists():
            continue
        try:
            skill = Skill.load(f)
            detect_files = _detect_files_of(skill)
        except Exception as e:
            out.append({"slug": d.name, "error": str(e)})
            continue
        out.append({
            "slug": d.name,
            "name": skill.name,
            "description": skill.description or "",
            "detect_files": detect_files,
        })
    return out


def detect_modifiers(repo_dir) -> list[str]:
    """Which stack modifiers apply to the repo cloned at `repo_dir`.

    A modifier applies when any of its `detect_files` exists at the repo root
    or one folder down (so `backend/pyproject.toml` in a monorepo counts).
    Results are alphabetical and capped at MAX_MODIFIERS, so the same repo
    always gives the same answer."""
    repo_dir = Path(repo_dir)
    folders = [repo_dir]
    try:
        for child in sorted(repo_dir.iterdir()):
            if (
                child.is_dir()
                and not child.is_symlink()
                and not child.name.startswith(".")
                and child.name not in _SKIP_DIRS
            ):
                folders.append(child)
    except OSError:
        logger.warning(
            "Could not inspect repository folders for stack modifiers: %s",
            repo_dir,
        )

    found = []
    for entry in list_modifiers():
        if "error" in entry:
            logger.warning(
                "Skipping invalid stack modifier %s: %s",
                entry["slug"],
                entry["error"],
            )
            continue
        if any(
            (marker_path := folder / marker).is_file() and not marker_path.is_symlink()
            for folder in folders
            for marker in entry["detect_files"]
        ):
            found.append(entry["slug"])
    return found[:MAX_MODIFIERS]


def render_modifiers(slugs: list[str], **variables: str) -> str:
    """Load each modifier, substitute {{VAR}} placeholders (same contract as
    render_skill), and join them into one text block. A modifier that fails
    to load is skipped with a warning so the pipeline can continue."""
    parts = []
    for slug in slugs:
        try:
            content = Skill.load(_modifier_path(slug)).content
        except Exception as e:
            logger.warning(
                "Skipping stack modifier %s because it could not be loaded: %s",
                slug,
                e,
            )
            continue
        for name, value in variables.items():
            content = content.replace(f"{{{{{name}}}}}", value)
        parts.append(f"[{slug}]\n{content.strip()}")
    return "\n\n".join(parts)