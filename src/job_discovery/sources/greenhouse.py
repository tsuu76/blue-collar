"""
Greenhouse Job Board API adapter.

Public, unauthenticated, officially documented by Greenhouse itself:
https://developers.greenhouse.io/job-board.html — "The Job Board API is
designed to export information about your public job boards and job posts
so ... developers can build custom career and application sites." GET
endpoints need no authentication; this is the exact endpoint a company's
own embedded careers page uses, so a request here is indistinguishable
from — and no more automated than — any browser loading that page.

Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
`identifier` in discover() is the board_token (e.g. "canva" for
boards.greenhouse.io/canva).
"""
from __future__ import annotations

import logging

from src.sources.base import NormalizedJob

from ..base import JobDiscoverySource, polite_get, strip_html

logger = logging.getLogger("job_hunter.job_discovery.greenhouse")

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseSource(JobDiscoverySource):
    platform = "greenhouse"

    def discover(self, identifier: str) -> list[NormalizedJob]:
        url = f"{API_BASE}/{identifier}/jobs?content=true"
        resp = polite_get(url)
        resp.raise_for_status()
        data = resp.json()

        jobs: list[NormalizedJob] = []
        for raw in data.get("jobs", []):
            try:
                jobs.append(_normalize(raw, identifier))
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed Greenhouse job from board %r: %s", identifier, exc)
        return jobs


def _normalize(raw: dict, board_token: str) -> NormalizedJob:
    location = ((raw.get("location") or {}).get("name") or "").strip()
    return NormalizedJob(
        source="greenhouse",
        url=raw.get("absolute_url", ""),
        title=raw["title"].strip(),
        description=strip_html(raw.get("content", "")),
        company=(raw.get("company_name") or board_token).strip(),
        location=location,
        source_job_id=str(raw["id"]),
    )
