"""
Phase 8 — AI job analysis.

Runs ONLY on jobs that already passed the deterministic cheap filter (see
src/job_filter/cheap_filter.py) — the whole point of that earlier stage is
to keep expensive local-LLM calls off obviously-senior or non-IT postings.

This module never trusts the model's JSON output at face value: every
response is validated against the strict JobAnalysis schema
(src/ai/schemas.py), and a response that parses as JSON but has the wrong
shape (bad category, fit_score out of range, etc.) is retried like any other
failure. If every retry is exhausted, this raises rather than returning
guessed/partial data — the caller must handle that by leaving the job
unanalyzed (status stays NEW/ANALYZING) rather than assuming a fit score
that was never actually produced.
"""
from __future__ import annotations

import logging

from pydantic import ValidationError

from src.config import settings

from .base import AIProvider, AIResponseError
from .factory import get_ai_provider
from .schemas import JobAnalysis

logger = logging.getLogger("job_hunter.ai.job_analysis")

SYSTEM_PROMPT = (
    "You are an assistant helping an entry-level IT job seeker in Australia evaluate job "
    "listings. Be honest and conservative — do not inflate fit scores, and only list a skill "
    "as 'matched' if the candidate's actual skill list (given below) supports it. Never assume "
    "the candidate has a skill just because the job asks for it."
)

_PROMPT_TEMPLATE = """Analyze this job listing for an entry-level IT candidate.

CANDIDATE'S ACTUAL SKILLS (only these — do not assume any others):
{candidate_skills}

JOB TITLE: {title}

JOB DESCRIPTION:
{description}

Return a JSON object with EXACTLY this shape (no extra keys, no missing keys):
{{
  "category": "ENTRY_LEVEL_IT" | "MID_SENIOR_IT" | "NON_IT" | "UNCLEAR",
  "fit_score": <integer 0-100>,
  "experience_required": "<short string, e.g. '0-1 years'>",
  "desk_based": <true|false>,
  "recommendation": "APPLY" | "SKIP" | "REVIEW",
  "matched_skills": [<candidate skills that genuinely apply to this job>],
  "missing_skills": [<skills this job wants that the candidate does NOT have>],
  "relevant_keywords": [<notable keywords from the job description>],
  "reasons": [<short factual reasons for the score/recommendation>],
  "concerns": [<anything that gives you pause, e.g. seniority creep, unclear duties>]
}}
"""


def build_prompt(title: str, description: str, candidate_skills: list[str]) -> str:
    skills_str = ", ".join(sorted(set(candidate_skills))) or "(none listed)"
    return _PROMPT_TEMPLATE.format(title=title, description=description, candidate_skills=skills_str)


def analyze_job(
    *,
    title: str,
    description: str,
    candidate_skills: list[str],
    provider: AIProvider | None = None,
    max_retries: int = settings.ollama_max_retries,
) -> JobAnalysis:
    """
    Run AI job analysis and return a validated JobAnalysis.

    Raises AIResponseError if the model cannot produce a valid, correctly-shaped
    response within max_retries + 1 attempts. Callers must catch this and fail
    safely (leave the job unanalyzed) rather than proceeding with bad data.
    """
    ai = provider or get_ai_provider()
    prompt = build_prompt(title, description, candidate_skills)

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = ai.generate_json(prompt, system=SYSTEM_PROMPT, max_retries=0)
            return JobAnalysis.model_validate(raw)
        except (AIResponseError, ValidationError) as exc:
            last_error = exc
            logger.warning(
                "Job analysis attempt %d/%d failed shape validation for %r: %s",
                attempt + 1,
                max_retries + 1,
                title,
                exc,
            )

    raise AIResponseError(
        f"AI job analysis failed for {title!r} after {max_retries + 1} attempts: {last_error}"
    )
