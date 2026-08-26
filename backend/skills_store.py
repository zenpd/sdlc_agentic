"""Persona skill discovery, selection, and rendering.

Skills live at ../skills/personas/<slug>/SKILL.md relative to this file, in
the OpenHands SDK's own AgentSkills format (openhands.sdk.skills.Skill),
parsed with the SDK's own loader rather than a bespoke one — see
skills/README.md for the full frontmatter/variable contract. Kept as plain
functions (no classes, no ORM) to match runs_db.py's style.
"""

import tempfile
from pathlib import Path
from typing import Optional

from openhands.sdk.skills import Skill
from openhands.sdk.skills.trigger import KeywordTrigger


SKILLS_DIR = Path(__file__).parent.parent / "skills" / "personas"
DEFAULT_PERSONA = "solution-architect"

# The {{VAR}} contract documented in skills/README.md — kept here as the
# single source of truth for what render_skill() accepts.
VARIABLE_NAMES = ["TICKET_KEY", "TICKET_SUMMARY", "TARGET_REPO", "TARGET_BRANCH", "PERSONA"]


class SkillValidationError(Exception):
    """Raised when a skill's content fails to parse as a valid SKILL.md."""


def _skill_path(slug: str) -> Path:
    return SKILLS_DIR / slug / "SKILL.md"


def _keywords_of(skill: Skill) -> list[str]:
    return skill.trigger.keywords if isinstance(skill.trigger, KeywordTrigger) else []


def list_skills() -> list[dict]:
    """List all persona skills with their parsed frontmatter metadata.

    A skill whose file fails to parse is still listed (with an "error" key)
    rather than silently dropped, so a bad edit is visible in the UI instead
    of just disappearing from the list.
    """
    out = []
    if not SKILLS_DIR.exists():
        return out
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        f = d / "SKILL.md"
        if not f.exists():
            continue
        try:
            skill = Skill.load(f)
        except Exception as e:
            out.append({"slug": d.name, "error": str(e)})
            continue
        out.append({
            "slug": d.name,
            "name": skill.name,
            "description": skill.description or "",
            "sdlc_stage": (skill.metadata or {}).get("sdlc_stage", ""),
            "keywords": _keywords_of(skill),
        })
    return out


def read_skill(slug: str) -> str:
    """Raw file content (frontmatter + body) for editing."""
    path = _skill_path(slug)
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
    return skill


def write_skill(slug: str, content: str) -> None:
    """Validate then persist a skill's content. Never writes a file that
    fails to parse — an existing skill is left untouched on a bad save."""
    _validate_content(slug, content)
    path = _skill_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_skill(slug: str, content: str) -> None:
    """Like write_skill, but refuses to overwrite an existing persona."""
    if _skill_path(slug).exists():
        raise FileExistsError(f"Skill {slug!r} already exists")
    write_skill(slug, content)


def select_persona(summary: str, description: str) -> str:
    """Score a ticket's text against each skill's trigger keywords, return
    the best-matching persona slug — falls back to DEFAULT_PERSONA when
    nothing matches, so a run always has a persona rather than none.

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
    for entry in list_skills():
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
    return best_slug or DEFAULT_PERSONA


def render_skill(slug: str, **variables: str) -> str:
    """Load a skill and substitute {{VAR}} placeholders in its body (the
    frontmatter-stripped content) with the given values."""
    skill = Skill.load(_skill_path(slug))
    content = skill.content
    for name, value in variables.items():
        content = content.replace(f"{{{{{name}}}}}", value)
    return content
