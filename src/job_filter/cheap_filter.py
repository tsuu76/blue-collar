"""
Deterministic ("cheap") pre-filter — runs before any Ollama call.

Per the spec, this must reject obvious senior/non-IT jobs cheaply, so the
expensive local-AI analysis stage only ever sees jobs that already look like
plausible entry-level IT roles. A hard seniority/experience rejection here
is final: it is never overridden by a later high skills-match score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.config import settings

from .experience import extract_min_years_required
from .keywords import (
    NEGATIVE_EXPERIENCE_KEYWORDS,
    NEGATIVE_NON_IT_KEYWORDS,
    NEGATIVE_SENIORITY_KEYWORDS,
    POSITIVE_EXPERIENCE_KEYWORDS,
    POSITIVE_TITLE_KEYWORDS,
)
from .location import location_matches


@dataclass
class CheapFilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)      # why it was rejected, or notable positive signals
    matched_positive_keywords: list[str] = field(default_factory=list)
    rejection_category: str | None = None  # "SENIORITY" | "EXPERIENCE" | "NON_IT" | "NO_POSITIVE_MATCH" | None
    min_years_required: int | None = None
    location_ok: bool = False


def _find_matches(haystack: str, needles: list[str]) -> list[str]:
    """
    Word-boundary keyword matching. Plain substring matching would let
    "director" match inside "Active Directory", or "l2" match inside an
    unrelated word — both false positives that would wrongly reject a good
    entry-level job. \\b correctly treats word boundaries even for phrases
    containing spaces/punctuation (e.g. "3+ years", "team lead").
    """
    haystack_lower = haystack.lower()
    matches = []
    for needle in needles:
        pattern = r"\b" + re.escape(needle.lower().strip()) + r"\b"
        if re.search(pattern, haystack_lower):
            matches.append(needle)
    return matches


def run_cheap_filter(
    title: str,
    description: str,
    location: str = "",
    *,
    max_experience_years: int | None = None,
    target_locations: list[str] | None = None,
) -> CheapFilterResult:
    """
    Evaluate a job against the deterministic rules. Order matters: hard
    rejects (seniority, non-IT, excess experience) short-circuit before we
    even bother checking for positive keyword matches, matching the spec's
    instruction not to let a good skills/keyword match rescue a senior job.
    """
    max_years = max_experience_years if max_experience_years is not None else settings.max_experience_years
    locations = target_locations if target_locations is not None else settings.target_locations

    combined_text = f"{title}\n{description}"

    seniority_hits = _find_matches(combined_text, NEGATIVE_SENIORITY_KEYWORDS)
    if seniority_hits:
        return CheapFilterResult(
            passed=False,
            reasons=[f"Rejected: seniority keyword(s) found: {seniority_hits}"],
            rejection_category="SENIORITY",
            location_ok=location_matches(location, locations),
        )

    non_it_hits = _find_matches(combined_text, NEGATIVE_NON_IT_KEYWORDS)
    if non_it_hits:
        return CheapFilterResult(
            passed=False,
            reasons=[f"Rejected: non-IT role keyword(s) found: {non_it_hits}"],
            rejection_category="NON_IT",
            location_ok=location_matches(location, locations),
        )

    experience_phrase_hits = _find_matches(combined_text, NEGATIVE_EXPERIENCE_KEYWORDS)
    min_years = extract_min_years_required(combined_text)
    experience_exceeded = min_years is not None and min_years > max_years

    if experience_phrase_hits or experience_exceeded:
        reason_bits = []
        if experience_phrase_hits:
            reason_bits.append(f"phrase(s): {experience_phrase_hits}")
        if experience_exceeded:
            reason_bits.append(f"requires {min_years}+ years, max allowed is {max_years}")
        return CheapFilterResult(
            passed=False,
            reasons=[f"Rejected: excess experience required — {'; '.join(reason_bits)}"],
            rejection_category="EXPERIENCE",
            min_years_required=min_years,
            location_ok=location_matches(location, locations),
        )

    positive_title_hits = _find_matches(combined_text, POSITIVE_TITLE_KEYWORDS)
    positive_experience_hits = _find_matches(combined_text, POSITIVE_EXPERIENCE_KEYWORDS)

    if not positive_title_hits:
        return CheapFilterResult(
            passed=False,
            reasons=["Rejected: no entry-level IT desk-role keyword found in title/description"],
            rejection_category="NO_POSITIVE_MATCH",
            min_years_required=min_years,
            location_ok=location_matches(location, locations),
        )

    reasons = [f"Matched IT role keyword(s): {positive_title_hits}"]
    if positive_experience_hits:
        reasons.append(f"Matched entry-level phrasing: {positive_experience_hits}")

    return CheapFilterResult(
        passed=True,
        reasons=reasons,
        matched_positive_keywords=positive_title_hits + positive_experience_hits,
        min_years_required=min_years,
        location_ok=location_matches(location, locations),
    )
