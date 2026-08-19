"""
Ashby Job Postings API adapter.

Public, unauthenticated, officially documented by Ashby:
https://developers.ashbyhq.com/docs/public-job-posting-api — "Get data for
all currently published Job Postings for your organization. If you host
your own careers page, you can use this data to populate it." Exactly the
intended third-party-consumption use case.

Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{clientname}
`identifier` in discover() is the clientname (Ashby's board slug).
"""
from __future__ import annotations

import logging

from src.sources.base import NormalizedJob

from ..base import JobDiscoverySource, polite_get, strip_html

logger = logging.getLogger("job_hunter.job_discovery.ashby")

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbySource(JobDiscoverySource):
    platform = "ashby"

    def discover(self, identifier: str) -> list[NormalizedJob]:
        url = f"{API_BASE}/{identifier}"
        resp = polite_get(url)
        resp.raise_for_status()
        data = resp.json()

        jobs: list[NormalizedJob] = []
        for raw in data.get("jobs", []):
            try:
                jobs.append(_normalize(raw, identifier))
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed Ashby job from %r: %s", identifier, exc)
        return jobs


def _normalize(raw: dict, board_identifier: str) -> NormalizedJob:
    description = raw.get("descriptionPlain") or strip_html(raw.get("descriptionHtml", ""))
    return NormalizedJob(
        source="ashby",
        url=raw.get("jobUrl") or raw.get("applyUrl", ""),
        title=raw["title"].strip(),
        description=description,
        company=board_identifier,
        location=(raw.get("location") or "").strip(),
        source_job_id=str(raw["id"]),
    )
