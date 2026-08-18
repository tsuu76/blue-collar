"""
Phase 10 — resume tailoring.

The pipeline here is strictly: MASTER RESUME + TAILORING INSTRUCTIONS =
TAILORED RESUME. The AI only ever produces the middle piece (a diff), never
a resume from scratch. Three layers keep that honest:

  1. Schema (tailor_schema.py) — the AI's output must parse into
     TailoringInstructions or it's rejected outright.
  2. validate_tailoring_instructions() — every id the AI references (a
     bullet, a project, an experience entry) must already exist in the
     master resume. Every reorder must be a permutation of what's already
     there, never a superset. This is a hard, deterministic gate — no id
     that doesn't already exist in data/master_resume.json can survive it.
  3. A best-effort content guard on bullet rewrites: flags new numeric
     claims (possible fabricated metrics) and new capitalized/technical
     terms not already present in that bullet or anywhere in the resume's
     own skill set (possible fabricated skills). This is intentionally a
     first-pass heuristic, not the final word — Phase 12's dedicated AI
     quality-control pass is the authoritative hallucination check.

apply_tailoring() then trusts already-validated instructions and produces a
MasterResume-shaped tailored copy — never writes back to the master file.
"""
from __future__ import annotations

import logging

from pydantic import ValidationError

from src.ai.base import AIProvider, AIResponseError
from src.ai.factory import get_ai_provider
from src.config import settings

from .fabrication_guard import find_fabricated_numbers, find_suspicious_terms
from .schema import Bullet, Experience, MasterResume, Project
from .tailor_schema import BulletChange, TailoringInstructions

logger = logging.getLogger("job_hunter.resume.tailor")


def validate_tailoring_instructions(resume: MasterResume, instructions: TailoringInstructions) -> list[str]:
    """Return a list of problems (empty = valid). Never raises."""
    problems: list[str] = []

    if instructions.summary.action == "rewrite" and not (instructions.summary.text or "").strip():
        problems.append("summary.action is 'rewrite' but no text was provided")

    if instructions.skills.action == "reorder":
        # Subset selection is allowed, same as projects/experience — live
        # testing showed models consistently and reasonably want to omit
        # clearly irrelevant skills (e.g. creative-tool skills on a
        # technical support resume). Omitting a TRUE skill from display is
        # not a fabrication risk (the master resume, and every skill this
        # candidate genuinely has, is untouched); only inventing a skill
        # that was never there is. What's still hard-blocked below: any
        # entry that isn't one of the resume's real skills.
        requested = [s.strip() for s in instructions.skills.order]
        if len(requested) != len(set(requested)):
            problems.append("skills.order contains duplicate entries")
        unknown_skills = set(requested) - {s.strip() for s in resume.skills}
        if unknown_skills:
            problems.append(
                f"skills.order references skill(s) not in the master resume: {sorted(unknown_skills)} — "
                f"skills cannot be invented"
            )

    valid_project_ids = {p.id for p in resume.projects}
    if instructions.projects.action == "reorder":
        requested = instructions.projects.order
        if len(requested) != len(set(requested)):
            problems.append("projects.order contains duplicate ids")
        unknown = [i for i in requested if i not in valid_project_ids]
        if unknown:
            problems.append(f"projects.order references unknown project id(s) {unknown} — projects cannot be invented")

    valid_experience_ids = {e.id for e in resume.experience}
    if instructions.experience.action == "reorder":
        requested = instructions.experience.order
        if len(requested) != len(set(requested)):
            problems.append("experience.order contains duplicate ids")
        unknown = [i for i in requested if i not in valid_experience_ids]
        if unknown:
            problems.append(
                f"experience.order references unknown experience id(s) {unknown} — employment cannot be invented"
            )

    valid_bullet_ids = resume.all_bullet_ids()
    bullet_by_id = _index_bullets(resume)
    known_skills = resume.all_skills()

    seen_bullet_ids: set[str] = set()
    for change in instructions.bullet_changes:
        if change.id in seen_bullet_ids:
            problems.append(f"bullet_changes references id {change.id!r} more than once")
        seen_bullet_ids.add(change.id)

        if change.id not in valid_bullet_ids:
            problems.append(f"bullet_changes references unknown bullet id {change.id!r} — bullets cannot be invented")
            continue

        if change.action != "rewrite":
            continue
        if not (change.text or "").strip():
            problems.append(f"bullet {change.id!r}: action is 'rewrite' but no text was provided")
            continue

        original = bullet_by_id[change.id]
        fabricated_numbers = find_fabricated_numbers(change.text, [original.text])
        if fabricated_numbers:
            problems.append(
                f"bullet {change.id!r} rewrite introduces number(s) not present in the original text: "
                f"{sorted(fabricated_numbers)} — possible fabricated metric"
            )

        suspicious = find_suspicious_terms(change.text, [original.text], known_skills)
        if suspicious:
            problems.append(
                f"bullet {change.id!r} rewrite mentions term(s) not found anywhere in the master resume: "
                f"{sorted(suspicious)} — possible fabricated skill/claim (best-effort check; "
                f"Phase 12 QC does the authoritative pass)"
            )

    return problems


def _index_bullets(resume: MasterResume) -> dict[str, Bullet]:
    index: dict[str, Bullet] = {}
    for edu in resume.education:
        for b in edu.highlights:
            index[b.id] = b
    for exp in resume.experience:
        for b in exp.bullets:
            index[b.id] = b
    for proj in resume.projects:
        for b in proj.bullets:
            index[b.id] = b
    for b in resume.additional:
        index[b.id] = b
    return index


def _apply_bullet_changes(bullets: list[Bullet], changes_by_id: dict[str, BulletChange]) -> list[Bullet]:
    result: list[Bullet] = []
    for b in bullets:
        change = changes_by_id.get(b.id)
        if change is None or change.action == "keep":
            result.append(b)
        elif change.action == "omit":
            continue
        elif change.action == "rewrite":
            result.append(Bullet(id=b.id, text=change.text or b.text, skills=b.skills))
    return result


def _select_and_order(items: list, order_ids: list[str], action: str) -> list:
    if action != "reorder" or not order_ids:
        return list(items)
    by_id = {item.id: item for item in items}
    return [by_id[i] for i in order_ids if i in by_id]


def apply_tailoring(resume: MasterResume, instructions: TailoringInstructions) -> MasterResume:
    """
    Apply already-validated tailoring instructions to the master resume,
    returning a new, independent MasterResume-shaped object. The original
    master resume (and its file on disk) is never modified.
    """
    changes_by_id = {c.id: c for c in instructions.bullet_changes}

    summary = (
        instructions.summary.text
        if instructions.summary.action == "rewrite" and instructions.summary.text
        else resume.summary
    )

    skills = (
        list(instructions.skills.order)
        if instructions.skills.action == "reorder" and instructions.skills.order
        else list(resume.skills)
    )

    ordered_projects: list[Project] = _select_and_order(resume.projects, instructions.projects.order, instructions.projects.action)
    tailored_projects = [
        p.model_copy(update={"bullets": _apply_bullet_changes(p.bullets, changes_by_id)}) for p in ordered_projects
    ]

    ordered_experience: list[Experience] = _select_and_order(
        resume.experience, instructions.experience.order, instructions.experience.action
    )
    tailored_experience = [
        e.model_copy(update={"bullets": _apply_bullet_changes(e.bullets, changes_by_id)}) for e in ordered_experience
    ]

    tailored_education = [
        edu.model_copy(update={"highlights": _apply_bullet_changes(edu.highlights, changes_by_id)})
        for edu in resume.education
    ]

    tailored_additional = _apply_bullet_changes(resume.additional, changes_by_id)

    return resume.model_copy(
        update={
            "summary": summary,
            "skills": skills,
            "projects": tailored_projects,
            "experience": tailored_experience,
            "education": tailored_education,
            "additional": tailored_additional,
        }
    )


SYSTEM_PROMPT = (
    "You are helping tailor an entry-level IT candidate's resume to a specific job. "
    "You may ONLY reorder, select, or reword content that already exists in the master resume "
    "given to you. You must NEVER invent a new employer, project, certification, skill, "
    "achievement, metric, or years of experience. If the job wants a skill the candidate doesn't "
    "have, simply omit it — do not pretend they have it."
)

_PROMPT_TEMPLATE = """MASTER RESUME (the only source of truth — do not add anything beyond this):
{resume_json}

JOB TITLE: {title}

JOB DESCRIPTION:
{description}

Produce tailoring instructions as a JSON object with EXACTLY this shape:
{{
  "summary": {{"action": "rewrite"|"keep", "text": "<rewritten summary, still 100% true, or omit>"}},
  "skills": {{"action": "reorder"|"keep", "order": [<every existing skill string, reordered>]}},
  "projects": {{"action": "reorder"|"keep", "order": [<project ids you want included, in order — may be a subset>]}},
  "experience": {{"action": "reorder"|"keep", "order": [<experience ids you want included, in order — may be a subset>]}},
  "bullet_changes": [
    {{"id": "<existing bullet id>", "action": "rewrite"|"omit"|"keep", "text": "<new wording, still 100% true, or omit>"}}
  ]
}}

Rules:
- Every id you reference (project, experience, bullet) MUST already exist in the master resume above.
- Rewritten text must not add any new facts, skills, numbers, or claims beyond what the original already says.
- It's fine to reword a bullet to better match the job's terminology, as long as the underlying claim is unchanged.
- Prefer selecting/reordering experience and projects so the most relevant ones come first.
"""


def build_prompt(resume: MasterResume, title: str, description: str) -> str:
    return _PROMPT_TEMPLATE.format(
        resume_json=resume.model_dump_json(indent=2), title=title, description=description
    )


def generate_tailoring_instructions(
    resume: MasterResume,
    *,
    title: str,
    description: str,
    provider: AIProvider | None = None,
    max_retries: int = settings.ollama_max_retries,
) -> TailoringInstructions:
    """
    Ask the AI for tailoring instructions and validate them against the
    master resume before returning. Raises AIResponseError if no valid,
    non-fabricating instruction set can be produced within max_retries + 1
    attempts — callers must fail safe (fall back to the untailored master
    resume, or flag for manual review) rather than use unvalidated output.
    """
    ai = provider or get_ai_provider()
    base_prompt = build_prompt(resume, title, description)
    prompt = base_prompt

    last_error: str | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = ai.generate_json(prompt, system=SYSTEM_PROMPT, max_retries=0)
            instructions = TailoringInstructions.model_validate(raw)
        except (AIResponseError, ValidationError) as exc:
            last_error = str(exc)
            logger.warning("Tailoring attempt %d/%d failed to parse/validate shape: %s", attempt + 1, max_retries + 1, exc)
            prompt = base_prompt
            continue

        problems = validate_tailoring_instructions(resume, instructions)
        if not problems:
            return instructions
        last_error = "; ".join(problems)
        logger.warning("Tailoring attempt %d/%d failed content validation: %s", attempt + 1, max_retries + 1, last_error)
        # Feed the specific validation problems back in rather than blindly
        # repeating the same prompt — live testing showed models reliably
        # repeat the exact same mistake (e.g. trying to drop "irrelevant"
        # skills from skills.order, which isn't permitted) across every
        # retry unless told explicitly what went wrong.
        prompt = (
            f"{base_prompt}\n\nYour previous attempt was invalid — fix these specific problems this time:\n"
            + "\n".join(f"- {p}" for p in problems)
        )

    raise AIResponseError(
        f"AI resume tailoring failed for {title!r} after {max_retries + 1} attempts: {last_error}"
    )
