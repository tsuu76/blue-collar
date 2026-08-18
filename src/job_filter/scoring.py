"""
Phase 9 — job scoring.

Combines the deterministic cheap-filter result with the AI job analysis into
one transparent, weighted fit score (spec section 16). Weights are
configurable via .env and are normalized here so they always sum to 1.0
even if a user's .env values don't add up exactly.

Hard rule, non-negotiable: a seniority or non-IT veto always wins over a
high skills-match score. A senior job with a 95% skill match must still be
REJECTED — this is enforced by capping the total score and setting
`passed=False` regardless of how the weighted components compute, not by
hoping the weights happen to work out that way.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.ai.schemas import JobAnalysis
from src.config import settings

from .cheap_filter import CheapFilterResult

VETO_CATEGORIES = {"MID_SENIOR_IT", "NON_IT"}


@dataclass
class JobScore:
    total_score: int  # 0-100, final rounded score
    component_scores: dict[str, float] = field(default_factory=dict)
    vetoed: bool = False
    veto_reason: str | None = None
    qualified: bool = False  # total_score >= min_fit_score AND not vetoed


def _normalized_weights(weights: dict[str, float] | None = None) -> dict[str, float]:
    w = weights or {
        "entry_level": settings.weight_entry_level,
        "it_relevance": settings.weight_it_relevance,
        "skills_match": settings.weight_skills_match,
        "experience_match": settings.weight_experience_match,
        "location": settings.weight_location,
    }
    total = sum(w.values())
    if total <= 0:
        raise ValueError("Scoring weights must sum to a positive number")
    return {k: v / total for k, v in w.items()}


def _entry_level_score(analysis: JobAnalysis) -> float:
    if analysis.category == "ENTRY_LEVEL_IT":
        return 100.0
    if analysis.category == "UNCLEAR":
        return 50.0
    return 0.0  # MID_SENIOR_IT, NON_IT


def _it_relevance_score(analysis: JobAnalysis, cheap_result: CheapFilterResult) -> float:
    base = {"ENTRY_LEVEL_IT": 90.0, "MID_SENIOR_IT": 70.0, "UNCLEAR": 40.0, "NON_IT": 0.0}[analysis.category]
    if analysis.desk_based:
        base = min(100.0, base + 10.0)
    # A small bonus for how many positive IT-role keywords the cheap filter
    # already found — caps out quickly so it can't dominate the AI's own
    # category judgement.
    keyword_bonus = min(10.0, len(cheap_result.matched_positive_keywords) * 2.0)
    return min(100.0, base * 0.9 + keyword_bonus)


def _skills_match_score(analysis: JobAnalysis) -> float:
    matched = len(analysis.matched_skills)
    missing = len(analysis.missing_skills)
    total = matched + missing
    if total == 0:
        return 50.0  # neutral — AI found nothing to compare, don't reward or punish
    return round(100.0 * matched / total, 2)


def _experience_match_score(cheap_result: CheapFilterResult, max_years: int) -> float:
    years = cheap_result.min_years_required
    if years is None:
        return 85.0  # unspecified — treated favorably per spec's "prefer no experience required"
    if years <= 0:
        return 100.0
    if years > max_years:
        return 0.0
    # Linear falloff from 100 (0 years) to 40 (at the configured cap).
    return round(100.0 - (60.0 * years / max_years), 2)


def _location_score(cheap_result: CheapFilterResult) -> float:
    return 100.0 if cheap_result.location_ok else 20.0


def score_job(
    cheap_result: CheapFilterResult,
    analysis: JobAnalysis,
    *,
    weights: dict[str, float] | None = None,
    min_fit_score: int | None = None,
    max_experience_years: int | None = None,
) -> JobScore:
    """
    Compute the final transparent, weighted fit score for a job that has
    already passed the cheap filter and been analyzed by the AI.

    The hard veto check runs first and is authoritative: if the AI itself
    concludes the job is MID_SENIOR_IT or NON_IT (even if the cheap filter's
    keyword rules missed it), the job is rejected outright — no weighted
    score, however high, can override that.
    """
    threshold = min_fit_score if min_fit_score is not None else settings.min_fit_score
    max_years = max_experience_years if max_experience_years is not None else settings.max_experience_years

    if not cheap_result.passed:
        return JobScore(
            total_score=0,
            vetoed=True,
            veto_reason=f"Cheap filter already rejected this job: {cheap_result.rejection_category}",
            qualified=False,
        )

    if analysis.category in VETO_CATEGORIES:
        # Still compute the components for transparency/debugging, but the
        # total is forced down regardless of what they say.
        components = {
            "entry_level": _entry_level_score(analysis),
            "it_relevance": _it_relevance_score(analysis, cheap_result),
            "skills_match": _skills_match_score(analysis),
            "experience_match": _experience_match_score(cheap_result, max_years),
            "location": _location_score(cheap_result),
        }
        return JobScore(
            total_score=0,
            component_scores=components,
            vetoed=True,
            veto_reason=f"AI analysis categorized this job as {analysis.category} — hard veto overrides skill match",
            qualified=False,
        )

    w = _normalized_weights(weights)
    components = {
        "entry_level": _entry_level_score(analysis),
        "it_relevance": _it_relevance_score(analysis, cheap_result),
        "skills_match": _skills_match_score(analysis),
        "experience_match": _experience_match_score(cheap_result, max_years),
        "location": _location_score(cheap_result),
    }
    weighted_total = (
        components["entry_level"] * w["entry_level"]
        + components["it_relevance"] * w["it_relevance"]
        + components["skills_match"] * w["skills_match"]
        + components["experience_match"] * w["experience_match"]
        + components["location"] * w["location"]
    )
    total_score = round(weighted_total)

    return JobScore(
        total_score=total_score,
        component_scores=components,
        vetoed=False,
        veto_reason=None,
        qualified=total_score >= threshold,
    )
