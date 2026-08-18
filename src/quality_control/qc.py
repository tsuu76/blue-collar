"""
Phase 12 — quality control (spec section 19).

This is the AUTHORITATIVE hallucination check, sitting after resume
tailoring (Phase 10) and cover letter generation (Phase 11), both of which
already run their own best-effort fabrication guards. QC re-examines the
finished tailored resume and cover letter together, against the master
resume, with fresh eyes.

Live testing against real Ollama output (llama3:latest) showed the AI QC
pass alone is NOT reliable for fact-membership checks — it repeatedly and
confidently claimed real facts were fabricated (e.g. "JavaScript skill not
supported by master resume" when JavaScript was right there in the skills
list it was given, and "Pizza Maker/Olympic Hotel experience not found in
master resume" for jobs that are real but simply weren't selected into this
particular tailored resume). An unreliable QC stage that constantly false-
positives is worse than useless: everything ends up flagged for manual
review, defeating the automation entirely.

So QC here is two layers, same pattern as the cheap-filter-before-AI design
in Phase 7:
  1. run_deterministic_checks() — code only, zero hallucination risk. Reuses
     the same fabrication_guard functions from Phases 10/11 to verify skill/
     term/number membership against the master resume, and checks required
     sections structurally. This is the AUTHORITATIVE answer for exactly the
     categories code can actually verify.
  2. The AI pass — explicitly told skill fabrication and missing sections
     are already handled, so it only has to judge what genuinely needs
     semantic reasoning: wrong dates, wrong company/title association,
     unsupported claims, and contradictions between the resume and cover
     letter. Narrowing its job is what makes its output trustworthy instead
     of noisy.

An application is only allowed to reach READY_TO_APPLY if both layers pass.
If not, run_quality_control_with_correction() regenerates the tailored
resume + cover letter (a fresh attempt, since we have no infrastructure for
surgically patching just the flagged issue) and re-checks, up to a bounded
number of attempts. If it still fails, the result is handed back with
needs_manual_review=True — this module never silently ships content that
failed its own check.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from src.ai.base import AIProvider, AIResponseError
from src.ai.factory import get_ai_provider
from src.ai.schemas import QualityControlResult
from src.config import settings
from src.resume.fabrication_guard import find_fabricated_numbers, find_suspicious_terms
from src.resume.schema import MasterResume

logger = logging.getLogger("job_hunter.quality_control")

SYSTEM_PROMPT = (
    "You are a fact-checker reviewing a job application before it is sent. Skill/technology fabrication, "
    "required-section completeness, and date/company/job-title accuracy have ALREADY been verified "
    "separately by a deterministic check — do NOT flag any of those, they are guaranteed correct. Your "
    "job is narrower and requires real judgment: (1) unsupported claims — vague exaggeration that isn't "
    "grounded in anything specific in the master resume, and (2) contradictions between the tailored "
    "resume and the cover letter — the two documents stating genuinely inconsistent things about the "
    "same fact. "
    "IMPORTANT — two things that are NOT problems and must never be flagged: "
    "(a) The tailored resume is expected to include only a SELECTED SUBSET of the master resume's "
    "employers/projects — omitting a real employer or project that exists in the master resume is "
    "intentional and correct, not an error, even if it's not mentioned anywhere in the tailored resume or "
    "cover letter. "
    "(b) The cover letter naturally paraphrases or references the job description's own wording (e.g. "
    "'the role's emphasis on scripting knowledge') — that is describing the JOB's requirements, not a "
    "claim the candidate possesses something; only flag it if the candidate is explicitly claiming to "
    "personally have a skill/experience that isn't in the master resume. "
    "Be precise — only flag something you can point to a specific, concrete mismatch for."
)

_PROMPT_TEMPLATE = """MASTER RESUME (ground truth — the only facts that may appear anywhere below):
{master_resume_json}

TAILORED RESUME being submitted:
{tailored_resume_json}

COVER LETTER being submitted:
{cover_letter}

JOB TITLE: {title}
COMPANY: {company}
JOB DESCRIPTION: {description}

Skill/technology fabrication, missing sections, and date/company/job-title accuracy are already checked
separately and guaranteed correct — do not flag those. Check only for:
- unsupported claims (vague exaggeration not grounded in anything specific in the master resume)
- contradictions between the tailored resume and the cover letter (the two documents genuinely
  disagreeing about the same fact — NOT the tailored resume simply omitting something real, which is
  expected and correct)

Return a JSON object with EXACTLY this shape:
{{
  "passed": true|false,
  "issues": [<short, specific description of each problem found, empty list if none>]
}}
"""


def build_prompt(
    master_resume: MasterResume, tailored_resume: MasterResume, cover_letter: str, *, title: str, company: str, description: str
) -> str:
    return _PROMPT_TEMPLATE.format(
        master_resume_json=master_resume.model_dump_json(indent=2),
        tailored_resume_json=tailored_resume.model_dump_json(indent=2),
        cover_letter=cover_letter,
        title=title,
        company=company,
        description=description,
    )


def _tailored_resume_text(resume: MasterResume) -> str:
    return "\n".join(
        [resume.summary]
        + resume.skills
        + [b.text for e in resume.experience for b in e.bullets]
        + [b.text for p in resume.projects for b in p.bullets]
        + [b.text for edu in resume.education for b in edu.highlights]
    )


def run_deterministic_checks(
    master_resume: MasterResume, tailored_resume: MasterResume, cover_letter: str, *, title: str, company: str, description: str
) -> list[str]:
    """
    Code-only checks, zero model-hallucination risk. See module docstring
    for why this exists — the AI QC pass alone proved unreliable for exactly
    these categories in live testing.

    title/company/description are included in the allowed pool because
    referencing the job's own terminology (the role title, the company
    being applied to) is expected and legitimate — only content that traces
    to neither the master resume nor the job posting is a fabrication.
    """
    issues: list[str] = []

    known_skills = master_resume.all_skills()
    allowed_texts = (
        master_resume.all_text_fragments()
        + master_resume.skills
        + master_resume.identifying_names()
        + [title, company, description]
    )

    for label, text in (("tailored resume", _tailored_resume_text(tailored_resume)), ("cover letter", cover_letter)):
        fabricated_numbers = find_fabricated_numbers(text, allowed_texts)
        if fabricated_numbers:
            issues.append(f"{label}: number(s) not traceable to the master resume: {sorted(fabricated_numbers)}")
        suspicious = find_suspicious_terms(text, allowed_texts, known_skills)
        if suspicious:
            issues.append(f"{label}: term(s) not found in the master resume: {sorted(suspicious)}")

    if not tailored_resume.personal.full_name or not tailored_resume.personal.email:
        issues.append("Tailored resume is missing required contact info (name/email)")
    if not tailored_resume.experience and not tailored_resume.projects:
        issues.append("Tailored resume has neither experience nor projects listed")
    if not tailored_resume.education:
        issues.append("Tailored resume is missing an education section")

    issues.extend(_check_dates_and_titles_match(master_resume, tailored_resume))

    return issues


def _check_dates_and_titles_match(master_resume: MasterResume, tailored_resume: MasterResume) -> list[str]:
    """
    For every experience/project the tailored resume includes, verify its
    company/title/dates match the master resume entry with the same id
    exactly. apply_tailoring() (Phase 10) only reorders/selects/copies
    existing objects and never mutates company/title/date fields, so this
    should always come back clean by construction — it exists as a genuine
    safety net (catches a real bug in apply_tailoring, should one ever be
    introduced) and, just as importantly, gives us a verified guarantee we
    can act on: since this is checked deterministically, the AI QC prompt
    doesn't need to re-check dates/company/title pairing at all.
    """
    issues: list[str] = []
    master_experience_by_id = {e.id: e for e in master_resume.experience}
    for exp in tailored_resume.experience:
        master_exp = master_experience_by_id.get(exp.id)
        if master_exp is None:
            issues.append(f"Tailored resume references experience id {exp.id!r} not found in master resume")
            continue
        if (exp.company, exp.title, exp.start_date, exp.end_date) != (
            master_exp.company,
            master_exp.title,
            master_exp.start_date,
            master_exp.end_date,
        ):
            issues.append(f"Tailored resume experience {exp.id!r} does not match master resume company/title/dates")

    master_projects_by_id = {p.id: p for p in master_resume.projects}
    for proj in tailored_resume.projects:
        master_proj = master_projects_by_id.get(proj.id)
        if master_proj is None:
            issues.append(f"Tailored resume references project id {proj.id!r} not found in master resume")
            continue
        if proj.name != master_proj.name:
            issues.append(f"Tailored resume project {proj.id!r} does not match master resume project name")

    return issues


def run_quality_control(
    master_resume: MasterResume,
    tailored_resume: MasterResume,
    cover_letter: str,
    *,
    title: str,
    company: str,
    description: str,
    provider: AIProvider | None = None,
    max_retries: int = settings.ollama_max_retries,
) -> QualityControlResult:
    """
    Run one full QC pass: deterministic checks + the narrowed AI pass,
    combined into a single verdict. Deterministic issues are authoritative
    and always included; the AI call still runs regardless (even if
    deterministic checks already failed) so a single pass surfaces the
    complete picture rather than stopping at the first problem found.

    The AI call retries only on malformed/wrongly-shaped JSON — a cleanly-
    shaped {"passed": false, "issues": [...]} is a valid, successful QC run
    reporting real problems, not something to retry against the same
    unchanged inputs. Raises AIResponseError only if the model can never
    produce valid, correctly-shaped JSON at all.
    """
    deterministic_issues = run_deterministic_checks(
        master_resume, tailored_resume, cover_letter, title=title, company=company, description=description
    )

    ai = provider or get_ai_provider(model=settings.ollama_qc_model)
    prompt = build_prompt(master_resume, tailored_resume, cover_letter, title=title, company=company, description=description)

    last_error: str | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = ai.generate_json(prompt, system=SYSTEM_PROMPT, max_retries=0)
            ai_result = QualityControlResult.model_validate(raw)
            combined_issues = deterministic_issues + ai_result.issues
            return QualityControlResult(passed=not combined_issues, issues=combined_issues)
        except (AIResponseError, ValidationError) as exc:
            last_error = str(exc)
            logger.warning("QC attempt %d/%d failed to parse/validate shape: %s", attempt + 1, max_retries + 1, exc)

    raise AIResponseError(f"Quality control failed to produce valid JSON after {max_retries + 1} attempts: {last_error}")


@dataclass
class QCPipelineResult:
    passed: bool
    tailored_resume: MasterResume
    cover_letter: str
    qc: QualityControlResult
    attempts: int
    needs_manual_review: bool


def run_quality_control_with_correction(
    master_resume: MasterResume,
    *,
    title: str,
    company: str,
    description: str,
    tailoring_provider: AIProvider | None = None,
    cover_letter_provider: AIProvider | None = None,
    qc_provider: AIProvider | None = None,
    max_correction_attempts: int = 2,
) -> QCPipelineResult:
    """
    Full generate -> QC -> correct -> revalidate loop (spec section 19).

    Each correction attempt regenerates BOTH the tailored resume and the
    cover letter from scratch (through their own already-guarded generation
    functions) rather than trying to surgically patch just the flagged
    issue — we have no reliable way to target a fix at one specific
    complaint, and a fresh generation attempt already carries its own
    fabrication guards plus whatever randomness helps it avoid repeating the
    same mistake.

    If QC still fails after max_correction_attempts + 1 total attempts, the
    result comes back with needs_manual_review=True. Callers MUST treat that
    as "do not advance to READY_TO_APPLY" — this function never softens a
    failing verdict into a passing one.
    """
    # Imported here (not at module level) to avoid a needless import-order
    # dependency at module load time — these are only used inside this one
    # orchestration function.
    from src.cover_letter.generator import generate_cover_letter
    from src.resume.tailor import apply_tailoring, generate_tailoring_instructions

    tailored = master_resume
    letter = ""
    qc_result = QualityControlResult(passed=False, issues=["(no attempt made)"])

    for attempt in range(max_correction_attempts + 1):
        instructions = generate_tailoring_instructions(
            master_resume, title=title, description=description, provider=tailoring_provider
        )
        tailored = apply_tailoring(master_resume, instructions)
        letter = generate_cover_letter(
            tailored, title=title, company=company, description=description, provider=cover_letter_provider
        )
        qc_result = run_quality_control(
            master_resume, tailored, letter, title=title, company=company, description=description, provider=qc_provider
        )
        if qc_result.passed:
            return QCPipelineResult(
                passed=True,
                tailored_resume=tailored,
                cover_letter=letter,
                qc=qc_result,
                attempts=attempt + 1,
                needs_manual_review=False,
            )
        logger.warning(
            "QC failed on attempt %d/%d: %s", attempt + 1, max_correction_attempts + 1, qc_result.issues
        )

    return QCPipelineResult(
        passed=False,
        tailored_resume=tailored,
        cover_letter=letter,
        qc=qc_result,
        attempts=max_correction_attempts + 1,
        needs_manual_review=True,
    )
