"""
Numeric years-of-experience extraction from free-text job descriptions.

This generalizes beyond the fixed "5+ years" style phrases in keywords.py to
catch patterns like "requires 4 years of experience" or "3-5 years in a
similar role", so the experience filter isn't purely a keyword coincidence.
"""
from __future__ import annotations

import re

# Matches things like: "3+ years", "3 - 5 years", "at least 4 years",
# "minimum of 2 years", "2 years of experience"
_YEARS_PATTERNS = [
    re.compile(r"(\d+)\s*\+\s*years?"),
    re.compile(r"(\d+)\s*-\s*\d+\s*years?"),
    re.compile(r"(?:minimum|min\.?|at least)\s*(?:of\s*)?(\d+)\s*years?"),
    re.compile(r"(\d+)\s*years?\s*(?:of\s*)?(?:professional\s*)?experience"),
]


def extract_min_years_required(text: str) -> int | None:
    """
    Best-effort extraction of the minimum years of experience mentioned in a
    job description. Returns None if no such phrase is found (treated as
    "unspecified", not zero — an unspecified requirement should not be
    auto-rejected just because no number is present).
    """
    text_lower = text.lower()
    found: list[int] = []
    for pattern in _YEARS_PATTERNS:
        for match in pattern.finditer(text_lower):
            try:
                found.append(int(match.group(1)))
            except (ValueError, IndexError):
                continue
    if not found:
        return None
    return min(found)
