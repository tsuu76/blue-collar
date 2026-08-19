"""
Lever Postings API adapter.

Public, unauthenticated, officially documented by Lever in their own
GitHub repo: https://github.com/lever/postings-api — "Every Lever.co
customer has a public API that allows you to retrieve jobs with no
authentication required." This is the same endpoint Lever-hosted careers
pages themselves use to render their listings.

Endpoint: GET https://api.lever.co/v0/postings/{company}?mode=json
`identifier` in discover() is the company slug (e.g. "netflix" for
jobs.lever.co/netflix). Response is a bare JSON array, not wrapped in an
object.
"""
from __future__ import annotations

import logging

from src.sources.base import NormalizedJob

from ..base import JobDiscoverySource, polite_get, strip_html

logger = logging.getLogger("job_hunter.job_discovery.lever")

API_BASE = "https://api.lever.co/v0/postings"


class LeverSource(JobDiscoverySource):
    platform = "lever"

    def discover(self, identifier: str) -> list[NormalizedJob]:
        url = f"{API_BASE}/{identifier}?mode=json"
        resp = polite_get(url)
        resp.raise_for_status()
        data = resp.json()

        jobs: list[NormalizedJob] = []
        for raw in data:
            try:
                jobs.append(_normalize(raw, identifier))
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed Lever job from %r: %s", identifier, exc)
        return jobs


def _normalize(raw: dict, company_slug: str) -> NormalizedJob:
    categories = raw.get("categories") or {}
    location = (categories.get("location") or "").strip()
    description = raw.get("descriptionPlain") or strip_html(raw.get("description", ""))
    return NormalizedJob(
        source="lever",
        url=raw.get("hostedUrl", ""),
        title=raw["text"].strip(),
        description=description,
        company=(raw.get("company") or company_slug).strip(),
        location=location,
        source_job_id=str(raw["id"]),
    )
