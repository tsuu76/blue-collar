"""
Workday career-site adapter.

Workday hosts each employer's public careers site at
`https://{tenant}.wd{N}.myworkdayjobs.com/{site}`, and that site renders its
job list by calling one JSON endpoint on the same host:

    POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

Unauthenticated, no key, no cookie — it is the same request the employer's
own careers page makes for every visitor who loads it, returning exactly the
jobs that page displays. Reading it is the Workday equivalent of the
Greenhouse adapter reading Greenhouse's board API.

`identifier` is NOT a company name or a guessed slug. It is the canonical
career-site URL that src/job_discovery/careers_resolver.py extracted from a
Workday link found in the company's own HTML:

    https://{tenant}.wd{N}.myworkdayjobs.com/{tenant}/{site}

If that link was never present on the company's site, this adapter is never
reached — there is no path in this project that assembles a Workday tenant
from a company name.

Two deliberate limits:

  * **Paging is capped** at MAX_PAGES pages (Workday caps `limit` at 20, so
    that is MAX_PAGES * 20 postings). A handful of very large employers
    advertise hundreds of roles at once; reading every page of every one of
    them, at this project's per-host rate limit, would turn a discovery run
    into an overnight job for no added signal. `total` from the response is
    logged so the real figure is never lost.
  * **Descriptions are not fetched.** Workday serves the body of a posting
    from a second endpoint, one request per job. That is one request per
    posting per company, which is not a proportionate amount of traffic to
    put on an employer's site for a discovery sweep. Titles, locations and
    URLs come from the list endpoint, and anything downstream that needs the
    full text has the real URL to fetch it from.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from src.sources.base import NormalizedJob

from ..base import JobDiscoverySource, polite_post

logger = logging.getLogger("job_hunter.job_discovery.workday")

PAGE_LIMIT = 20
MAX_PAGES = 3
REQUEST_TIMEOUT_SECONDS = 15

_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)\.(wd\d+)\.(myworkdayjobs\.com|myworkday\.com)$", re.I)


def parse_identifier(identifier: str) -> tuple[str, str, str] | None:
    """
    Split a resolver-produced career-site URL into (host, tenant, site).

    Accepts the canonical form this project stores,
    `https://{tenant}.wd3.myworkdayjobs.com/{tenant}/{site}`, and also the
    plainer `https://{tenant}.wd3.myworkdayjobs.com/{site}` and localised
    `.../en-US/{site}` forms, so an identifier pasted straight from a
    browser address bar works too. Returns None for anything that is not a
    Workday career-site URL — never a partial guess.
    """
    parsed = urlparse((identifier or "").strip())
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.netloc or "").lower()
    match = _HOST_RE.match(host)
    if not match:
        return None
    tenant = match.group(1)

    segments = [s for s in parsed.path.split("/") if s]
    segments = [s for s in segments if not re.fullmatch(r"[a-z]{2}([-_][A-Za-z]{2})?", s)]
    # The canonical form repeats the tenant before the site name; strip that
    # prefix only when something follows it, since a career site legitimately
    # named after its tenant (".../bhp/BHP" and ".../BHP" alike) must not be
    # stripped down to nothing.
    if len(segments) > 1 and segments[0].lower() == tenant:
        segments = segments[1:]
    if not segments:
        return None
    return host, tenant, segments[0]


class WorkdaySource(JobDiscoverySource):
    platform = "workday"

    def discover(self, identifier: str) -> list[NormalizedJob]:
        parsed = parse_identifier(identifier)
        if parsed is None:
            logger.warning("Not a Workday career-site URL, refusing to guess one: %r", identifier)
            return []
        host, tenant, site = parsed
        endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

        jobs: list[NormalizedJob] = []
        seen_paths: set[str] = set()
        for page in range(MAX_PAGES):
            payload = {
                "appliedFacets": {},
                "limit": PAGE_LIMIT,
                "offset": page * PAGE_LIMIT,
                "searchText": "",
            }
            response = polite_post(
                endpoint,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                logger.warning("Unexpected Workday response shape for %s/%s", tenant, site)
                break

            postings = data.get("jobPostings")
            if not isinstance(postings, list) or not postings:
                break

            for raw in postings:
                job = _normalize(raw, host=host, site=site, tenant=tenant)
                if job is None:
                    continue
                if job.source_job_id in seen_paths:
                    continue
                seen_paths.add(job.source_job_id)
                jobs.append(job)

            total = data.get("total")
            if isinstance(total, int) and (page + 1) * PAGE_LIMIT >= total:
                break
            if page + 1 == MAX_PAGES and isinstance(total, int) and total > len(jobs):
                logger.info(
                    "Workday %s/%s advertises %d roles; read the first %d (paging cap).",
                    tenant, site, total, len(jobs),
                )
        return jobs


def _normalize(raw, *, host: str, site: str, tenant: str) -> NormalizedJob | None:
    """
    One posting, or None when the entry carries too little to be called a
    real posting. A missing title is never filled in with a placeholder and
    a missing path never becomes an invented URL — the entry is dropped.
    """
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    external_path = str(raw.get("externalPath") or "").strip()
    if not title or not external_path:
        logger.debug("Skipping incomplete Workday posting from %s/%s: %r", tenant, site, raw)
        return None
    if not external_path.startswith("/"):
        external_path = f"/{external_path}"

    return NormalizedJob(
        source="workday",
        url=f"https://{host}/{site}{external_path}",
        title=title,
        # The list endpoint carries no description; see the module docstring
        # for why this adapter does not fetch one per posting. An empty
        # string is the honest value — never a synthesised summary.
        description="",
        company=tenant,
        location=str(raw.get("locationsText") or "").strip(),
        source_job_id=external_path,
    )
