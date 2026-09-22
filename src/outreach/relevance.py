"""
AU relevance for outreach research.

Blue Collar is a Sydney/NSW-focused tool (see the pinned
`target-locations-are-a-hard-ui-default` memory). Outreach must honour
the same rule as the job-hunt UI: a company whose 609 current postings
are all in Amsterdam is not a Sydney outreach target no matter how big
or on-brand the company is.

This module answers one question: *of the real postings this run
found, how many are in the app's target region?* — using the same
match rule `src.job_filter.location.location_matches` uses, so the
outreach path and the job path can never disagree on what "in Sydney"
means.

The output is a small `LocationRelevance` object, not a hard yes/no —
callers still decide what to do with a company that has zero matches
(the pipeline may still drop them, but the raw evidence is preserved
so the dashboard can explain the decision).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.config import settings
from src.job_filter.location import location_matches


@dataclass(frozen=True)
class LocationRelevance:
    """
    Per-run summary of how many of a company's postings match the
    configured target locations. `total` is the number of postings
    inspected; `matching` is how many of those `location_matches`
    called a match. Never negative, `matching` never exceeds `total`.
    """

    matching: int
    total: int

    @property
    def any_match(self) -> bool:
        return self.matching > 0

    def to_dict(self) -> dict:
        return {"matching": self.matching, "total": self.total}


def au_relevance(
    posting_locations: Iterable[str],
    target_locations: list[str] | None = None,
) -> LocationRelevance:
    """
    Count how many of `posting_locations` match the configured target
    locations. `target_locations` defaults to `settings.target_locations`
    at call time so tests that swap that value in via monkeypatch see
    the new value immediately.

    Empty/blank posting locations count toward `total` (they exist in
    the DB and the caller needs to know the denominator), but they can
    never be a match — location_matches returns False for empty text.
    """
    targets = list(target_locations) if target_locations is not None else list(settings.target_locations)
    total = 0
    matching = 0
    for loc in posting_locations:
        total += 1
        if location_matches(loc or "", targets):
            matching += 1
    return LocationRelevance(matching=matching, total=total)
