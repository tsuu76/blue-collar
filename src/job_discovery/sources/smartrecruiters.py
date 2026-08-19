"""
SmartRecruiters Posting API adapter.

Public, unauthenticated (when the customer has it enabled — the default
for public postings), officially documented by SmartRecruiters:
https://developers.smartrecruiters.com/docs/posting-api — "The Posting API
allows access to job postings that were previously made public." This is
the same data SmartRecruiters-hosted careers pages themselves display.

List endpoint:   GET https://api.smartrecruiters.com/v1/companies/{company}/postings
Detail endpoint: GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}
                 (the list endpoint's entries are summaries; the full job
                 description (jobAd) needs the per-posting detail call.)
`identifier` in discover() is the company identifier used in that URL path.
"""
from __future__ import annotations

import logging

from src.sources.base import NormalizedJob

from ..base import JobDiscoverySource, polite_get, strip_html

logger = logging.getLogger("job_hunter.job_discovery.smartrecruiters")

API_BASE = "https://api.smartrecruiters.com/v1/companies"


class SmartRecruitersSource(JobDiscoverySource):
    platform = "smartrecruiters"

    def discover(self, identifier: str) -> list[NormalizedJob]:
        list_url = f"{API_BASE}/{identifier}/postings"
        resp = polite_get(list_url)
        resp.raise_for_status()
        data = resp.json()

        jobs: list[NormalizedJob] = []
        for summary in data.get("content", []):
            try:
                posting_id = str(summary["id"])
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping SmartRecruiters posting summary with no id from %r: %s", identifier, exc)
                continue
            try:
                detail_resp = polite_get(f"{API_BASE}/{identifier}/postings/{posting_id}")
                detail_resp.raise_for_status()
                detail = detail_resp.json()
                jobs.append(_normalize(summary, detail, identifier))
            except Exception as exc:  # noqa: BLE001 — one bad posting must not stop the rest
                logger.warning("Skipping SmartRecruiters posting %s from %r: %s", posting_id, identifier, exc)
        return jobs


def _normalize(summary: dict, detail: dict, company_identifier: str) -> NormalizedJob:
    location_parts = summary.get("location") or {}
    location = ", ".join(
        part for part in [location_parts.get("city"), location_parts.get("region"), location_parts.get("country")] if part
    )

    job_ad = detail.get("jobAd") or {}
    ad_sections = (job_ad.get("sections") or {})
    description_html = " ".join(
        (section.get("text") or "") for section in ad_sections.values() if isinstance(section, dict)
    )
    description = strip_html(description_html) or strip_html(summary.get("name", ""))

    ref_number = summary.get("refNumber")
    posting_id = str(summary["id"])
    apply_url = f"https://jobs.smartrecruiters.com/{company_identifier}/{ref_number or posting_id}"

    return NormalizedJob(
        source="smartrecruiters",
        url=apply_url,
        title=summary["name"].strip(),
        description=description,
        company=((summary.get("company") or {}).get("name") or company_identifier).strip(),
        location=location,
        source_job_id=posting_id,
    )
