"""
Phase 11 — cover letter generation.

Unlike the resume, a cover letter is generated fresh for every job (spec
section 9) — there's no diff/instructions structure to validate ids
against. Instead, grounding + validation work differently:

  - The prompt is built ONLY from the tailored resume's own text (summary,
    skills, every bullet across education/experience/projects) plus the
    job's own title/description — nothing else the model could treat as
    fact.
  - validate_cover_letter() reuses the same fabrication_guard heuristics as
    resume tailoring: any number or capitalized/technical term in the
    generated letter that doesn't trace back to the resume OR the job
    posting itself (referencing the job's own terminology is fine — e.g.
    the job title) is flagged as a possible fabrication.
  - Word count is checked against COVER_LETTER_MIN_WORDS/MAX_WORDS.
  - A short cliché/generic-AI-language check flags common AI-cover-letter
    phrasing so a rewrite can be requested instead of that just being how
    it reads (spec explicitly asks to avoid this).

As with Phase 8/10, generation retries on any validation failure and raises
AIResponseError (never returns unvalidated output) if retries are
exhausted — callers must fail safe, not use unchecked prose.
"""
from __future__ import annotations

import logging

from src.ai.base import AIProvider, AIResponseError
from src.ai.factory import get_ai_provider
from src.config import settings
from src.resume.fabrication_guard import find_fabricated_numbers, find_suspicious_terms
from src.resume.schema import MasterResume

logger = logging.getLogger("job_hunter.cover_letter")

# Common AI-cover-letter clichés the spec explicitly asks to avoid
# ("avoid generic AI language", "avoid exaggerated enthusiasm").
CLICHE_PHRASES = [
    "i am thrilled",
    "i am passionate about",
    "i am excited to apply",
    "dynamic environment",
    "fast-paced environment",
    "perfect fit",
    "leverage my skills",
    "wear many hats",
    "hit the ground running",
    "go-getter",
    "synergy",
    "i am confident that i",
    "i am writing to express my interest",
    "in today's fast-paced world",
    "i would be a valuable asset",
    "think outside the box",
]

SYSTEM_PROMPT = (
    "You write concise, natural, honest cover letters for an entry-level IT candidate applying to "
    "jobs in Australia. Use ONLY the facts given to you about the candidate — never invent an "
    "employer, project, skill, certification, achievement, or metric. Do not use generic AI cover "
    "letter phrasing or exaggerated enthusiasm ('I am thrilled', 'passionate about', 'dynamic "
    "environment', 'perfect fit', etc). Write like a real person, specific to this one role."
)

_PROMPT_TEMPLATE = """CANDIDATE FACTS (the only source of truth — do not add anything beyond this):

Summary: {summary}

Skills: {skills}

Experience and project highlights:
{bullets}

JOB TITLE: {title}
COMPANY: {company}

JOB DESCRIPTION:
{description}

Write a cover letter for this candidate applying to this job. Requirements:
- Aim for approximately {target_words} words. It MUST be at least {min_words} words and no more than
  {max_words} words — models tend to undershoot, so write generously and include specific detail rather
  than stopping early; a letter that reads a little long is far better than one that's too short.
- First person, a brief natural greeting is fine (no "Dear Hiring Manager" boilerplate needed).
- Specific to this role and company — reference the actual job title and something concrete from the description.
- Mention 2-3 genuinely relevant skills/projects/experience from the candidate facts above.
- Explain briefly why the candidate is suitable, without exaggerating.
- If the job wants a skill the candidate doesn't have, do not claim they have it — you may mention an
  adjacent transferable skill instead, or simply don't bring it up.
- Do NOT invent or assume anything about the company — its culture, values, size, reputation, mission,
  or reasons the candidate finds it appealing — beyond what the job description above literally states.
  If the job description doesn't mention the company's culture/values, don't praise or characterize them;
  focus on the role and the candidate's fit instead.
- No invented facts about the candidate OR the company. No generic AI-cover-letter phrasing.
- Output ONLY the cover letter body text — no subject line, no explanation, no markdown formatting.
"""


def _bullets_text(resume: MasterResume) -> str:
    lines: list[str] = []
    for exp in resume.experience:
        lines.append(f"- {exp.title} at {exp.company}:")
        for b in exp.bullets:
            lines.append(f"    - {b.text}")
    for proj in resume.projects:
        lines.append(f"- Project: {proj.name} ({', '.join(proj.technologies)}):")
        for b in proj.bullets:
            lines.append(f"    - {b.text}")
    for edu in resume.education:
        lines.append(f"- {edu.credential}, {edu.institution}:")
        for b in edu.highlights:
            lines.append(f"    - {b.text}")
    return "\n".join(lines) if lines else "(none)"


def build_prompt(resume: MasterResume, *, title: str, company: str, description: str) -> str:
    min_words = settings.cover_letter_min_words
    max_words = settings.cover_letter_max_words
    # Lean toward the upper-middle of the range, not the midpoint — models
    # reliably undershoot a stated target more often than they overshoot
    # one, so aiming a bit high in the prompt lands closer to the actual
    # allowed range in practice.
    target_words = round(min_words + (max_words - min_words) * 0.6)
    return _PROMPT_TEMPLATE.format(
        summary=resume.summary or "(none provided)",
        skills=", ".join(resume.skills) or "(none listed)",
        bullets=_bullets_text(resume),
        title=title,
        company=company,
        description=description,
        min_words=min_words,
        max_words=max_words,
        target_words=target_words,
    )


def _word_count(text: str) -> int:
    return len(text.split())


def validate_cover_letter(
    text: str,
    resume: MasterResume,
    *,
    title: str,
    company: str,
    description: str,
    min_words: int | None = None,
    max_words: int | None = None,
) -> list[str]:
    """Return a list of problems (empty = valid). Never raises."""
    problems: list[str] = []

    if not text or not text.strip():
        return ["Cover letter text is empty"]

    lo = min_words if min_words is not None else settings.cover_letter_min_words
    hi = max_words if max_words is not None else settings.cover_letter_max_words
    count = _word_count(text)
    if count < lo:
        problems.append(f"Cover letter is too short: {count} words (minimum {lo})")
    if count > hi:
        problems.append(f"Cover letter is too long: {count} words (maximum {hi})")

    lower_text = text.lower()
    found_cliches = [p for p in CLICHE_PHRASES if p in lower_text]
    if found_cliches:
        problems.append(f"Contains generic/cliché phrasing: {found_cliches}")

    # Grounding pool: everything true about the candidate (including proper
    # nouns like company/project/institution names, which rarely appear
    # inside bullet prose itself), plus the job's own title/description
    # (referencing the role's own terminology is fine — e.g. saying the job
    # title, or noting the job needs X).
    allowed_texts = (
        resume.all_text_fragments() + resume.skills + resume.identifying_names() + [title, company, description]
    )

    fabricated_numbers = find_fabricated_numbers(text, allowed_texts)
    if fabricated_numbers:
        problems.append(
            f"Cover letter contains number(s) not traceable to the resume or job posting: "
            f"{sorted(fabricated_numbers)} — possible fabricated metric"
        )

    known_skills = resume.all_skills()
    suspicious = find_suspicious_terms(text, allowed_texts, known_skills)
    if suspicious:
        problems.append(
            f"Cover letter mentions term(s) not found in the resume or job posting: "
            f"{sorted(suspicious)} — possible fabricated skill/claim (best-effort check; "
            f"Phase 12 QC does the authoritative pass)"
        )

    return problems


def generate_cover_letter(
    resume: MasterResume,
    *,
    title: str,
    company: str,
    description: str,
    provider: AIProvider | None = None,
    max_retries: int = 2,
) -> str:
    """
    Generate and validate a cover letter. Raises AIResponseError if no
    valid, non-fabricating, correctly-sized letter can be produced within
    max_retries + 1 attempts — callers must fail safe (leave the
    application in a flagged/manual-review state) rather than use
    unvalidated prose.
    """
    ai = provider or get_ai_provider()
    prompt = build_prompt(resume, title=title, company=company, description=description)

    last_problems: list[str] = ["(no attempts made)"]
    for attempt in range(max_retries + 1):
        try:
            text = ai.generate(prompt, system=SYSTEM_PROMPT, temperature=0.4).strip()
        except AIResponseError as exc:
            last_problems = [str(exc)]
            logger.warning("Cover letter attempt %d/%d failed to generate: %s", attempt + 1, max_retries + 1, exc)
            continue

        problems = validate_cover_letter(text, resume, title=title, company=company, description=description)
        if not problems:
            return text
        last_problems = problems
        logger.warning(
            "Cover letter attempt %d/%d failed validation: %s", attempt + 1, max_retries + 1, "; ".join(problems)
        )

    raise AIResponseError(
        f"Cover letter generation failed for {title!r} at {company!r} after {max_retries + 1} attempts: "
        f"{'; '.join(last_problems)}"
    )
