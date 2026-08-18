"""
Manual job import — the reliable, always-available Type B intake path.

Two entry points:
  - normalize_manual_job(): the user (via the dashboard or a script) supplies
    title/company/location/url/description explicitly. This is the primary,
    trustworthy path.
  - parse_pasted_text(): best-effort heuristic extraction of title/company
    from a single pasted blob of job-ad text, for convenience when the user
    just pastes a whole ad. This is explicitly a *helper*, not authoritative
    — its output is meant to be reviewed/corrected by the user before
    saving, consistent with the "human reviews" design principle. It never
    invents a company/title it can't find; it leaves those fields blank
    rather than guessing wrong.
"""
from __future__ import annotations

import re

from .base import NormalizedJob

_URL_RE = re.compile(r"https?://\S+")


def normalize_manual_job(
    *,
    title: str,
    description: str,
    url: str = "",
    company: str = "",
    location: str = "",
    salary: str = "",
    source: str = "manual_paste",
) -> NormalizedJob:
    """Build a NormalizedJob from explicit, user-supplied fields."""
    title = title.strip()
    description = description.strip()
    if not title:
        raise ValueError("title is required for a manually imported job")
    if not description:
        raise ValueError("description is required for a manually imported job")
    return NormalizedJob(
        source=source,
        url=url.strip(),
        title=title,
        description=description,
        company=company.strip(),
        location=location.strip(),
        salary=salary.strip(),
    )


def parse_pasted_text(raw_text: str, *, source_url: str = "") -> NormalizedJob:
    """
    Best-effort parse of a single pasted block of job-ad text.

    Heuristics only:
      - title: the first non-empty line, if it's short enough to plausibly be
        a title (<120 chars) and doesn't look like a sentence (no trailing
        period followed by more text on the same line).
      - a bare URL anywhere in the text, if `source_url` wasn't supplied.
      - everything else becomes the description as-is.

    Company/location are intentionally left blank when they can't be
    confidently identified — the caller/UI should prompt the user to fill
    them in rather than this function guessing and being wrong.
    """
    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    if not lines:
        raise ValueError("Cannot parse an empty pasted job description")

    title = ""
    first_line = lines[0]
    if len(first_line) <= 120 and not first_line.rstrip().endswith("."):
        title = first_line

    url = source_url.strip()
    if not url:
        match = _URL_RE.search(raw_text)
        if match:
            url = match.group(0).rstrip(").,")

    return NormalizedJob(
        source="manual_paste",
        url=url,
        title=title,
        description=raw_text.strip(),
        company="",
        location="",
        salary="",
    )
