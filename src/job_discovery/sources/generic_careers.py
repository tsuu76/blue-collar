"""
Generic careers-page source — the ATS-agnostic verification path.

The four adapters alongside this one (Greenhouse, Lever, Ashby,
SmartRecruiters) each talk to one platform's own documented API. Most
Australian employers are not on any of those four — Workday, PageUp,
SuccessFactors, Taleo, JobAdder, and countless custom career sites have no
shared, stable, unauthenticated API this project can call. What almost all
of them DO have is their own careers page, published for the public to read.

This module reads exactly that page, on the company's own verified domain,
and looks for the one thing that is both legitimate and structured:
schema.org `JobPosting` markup (JSON-LD), which employers embed specifically
so external systems — Google for Jobs foremost among them — can read their
current openings. Reading it is no more "scraping" than the Greenhouse
adapter reading Greenhouse's job-board API: both are public data a company
published for exactly this kind of third-party consumption.

When a page carries no such markup — a plain HTML listing, an
authentication-walled ATS widget, a page that simply doesn't say — this
module does NOT try to guess at job titles from unstructured HTML. Guessing
structure from prose risks manufacturing a "job" that doesn't really exist,
which is exactly what this project forbids. The honest answer in that case
is "jobs data unavailable", not "confirmed zero" and not an invented title.

Finding *which* page to read is no longer this module's job. A fixed list
of conventional paths on the root domain missed most large employers, whose
careers system lives behind a link rather than at a guessable path, so that
work now belongs to src/job_discovery/careers_resolver.py — a bounded crawl
that follows the company's own links and fingerprints the ATS it finds. This
module keeps what it was always good at: turning one page's HTML into real
postings, or honestly reporting that it could not.

Every request is robots.txt-checked before it is made, and goes through the
same rate-limited, identified `polite_get` every other adapter uses. Nothing
here ever fetches LinkedIn or any third-party site — only the company's own
domain (or a careers host that company's own pages linked to), and only a
domain the caller already believes is real (this module never guesses a
domain from a company name).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from src.sources.base import NormalizedJob

from ..base import JobDiscoverySource, polite_get, strip_html
from ..careers_resolver import (  # noqa: F401 — robots helpers are re-exported for callers/tests
    _robots_cache,
    detect_ats_in_html,
    resolve_careers,
    robots_allows,
)

logger = logging.getLogger("job_hunter.job_discovery.generic_careers")

MAX_PAGE_CHARS = 600_000
FETCH_TIMEOUT_SECONDS = 10

_LD_JSON_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S
)


# --------------------------------------------------------------------------
# JobPosting JSON-LD extraction
# --------------------------------------------------------------------------

def _iter_ld_json_blocks(html: str):
    for match in _LD_JSON_RE.finditer(html or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue


def _walk_for_job_postings(node) -> list[dict]:
    """
    JSON-LD is nested in several real shapes in the wild: a bare JobPosting
    object, a list of them, or an object with `@graph` holding a mix of
    types. This walks all of them rather than assuming one.
    """
    found: list[dict] = []
    if isinstance(node, dict):
        type_value = node.get("@type")
        types = type_value if isinstance(type_value, list) else [type_value]
        if any(isinstance(t, str) and t.lower() == "jobposting" for t in types):
            found.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found.extend(_walk_for_job_postings(item))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_for_job_postings(item))
    return found


def _location_from(raw: dict) -> str:
    location = raw.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return ""
    address = location.get("address")
    if not isinstance(address, dict):
        return str(location.get("name") or "")
    parts = [address.get("addressLocality"), address.get("addressRegion")]
    return ", ".join(p for p in parts if p)


def _job_id_from(raw: dict, title: str, url: str) -> str:
    identifier = raw.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    if identifier:
        return str(identifier)
    return hashlib.sha256(f"{title}|{url}".encode("utf-8")).hexdigest()[:16]


def parse_job_postings(html: str, page_url: str) -> list[NormalizedJob]:
    """
    Every genuinely well-formed JobPosting found on this page. A block
    missing a title is skipped — not enough evidence to call it a real
    posting — but nothing here invents a value for a field that is absent.
    """
    jobs: list[NormalizedJob] = []
    for block in _iter_ld_json_blocks(html):
        for raw in _walk_for_job_postings(block):
            title = str(raw.get("title") or raw.get("name") or "").strip()
            if not title:
                continue
            url = str(raw.get("url") or page_url)
            organization = raw.get("hiringOrganization")
            company = ""
            if isinstance(organization, dict):
                company = str(organization.get("name") or "").strip()
            jobs.append(
                NormalizedJob(
                    source="generic_careers_jsonld",
                    url=url,
                    title=title,
                    description=strip_html(str(raw.get("description") or "")),
                    company=company,
                    location=_location_from(raw),
                    source_job_id=_job_id_from(raw, title, url),
                )
            )
    return jobs


def detect_known_system(html: str) -> str:
    """
    The human name of the ATS this page embeds, if any — "Workday",
    "PageUp", "Taleo"... Purely informational, never used to extract or
    infer a posting.

    Delegates to the resolver's fingerprint table rather than keeping a
    second copy of it here: one list of what a Workday URL looks like, used
    both for naming a system and for extracting the identifier that queries
    it, so the two can never disagree.
    """
    match = detect_ats_in_html(html, "")
    return match.label if match else ""


# --------------------------------------------------------------------------
# The JobDiscoverySource adapter — fetches ONE already-known-good URL
# --------------------------------------------------------------------------

class GenericCareersSource(JobDiscoverySource):
    """
    Re-reads a specific careers-page URL a prior probe already confirmed for
    this company (see probe_careers_page below). `identifier` here is that
    URL, not a slug — this adapter does no path-guessing of its own; that
    happens once, at discovery time, and the working URL is what gets stored
    in config/outreach_companies.json for every run after that.
    """

    platform = "careers_page"

    def discover(self, identifier: str) -> list[NormalizedJob]:
        url = (identifier or "").strip()
        if not url:
            return []
        if not robots_allows(url):
            logger.info("robots.txt disallows fetching %s — no postings read.", url)
            return []
        response = polite_get(url, timeout=FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        html = (response.text or "")[:MAX_PAGE_CHARS]
        return parse_job_postings(html, url)


# --------------------------------------------------------------------------
# Discovery-time probing — tries a few conventional paths on a verified
# domain and reports what it honestly found
# --------------------------------------------------------------------------

@dataclass
class CareersProbeResult:
    """
    What one company's careers infrastructure actually turned out to be.

    The distinction that matters lives in `jobs_available` vs `reachable`:
    a real page this module could not machine-read is `reachable=True,
    jobs_available=False`, which must never be reported as "no openings".

    `platform` / `ats_identifier` are set only when the resolver recognised
    an ATS **and** this project has an adapter that can query it — they are
    what lets a company graduate from "careers page verified" to real job
    data. `evidence` records the hops that got here, so any registry entry
    can be traced back to the pages it came from.
    """

    website: str
    reachable: bool = False
    url: str = ""
    postings: list[NormalizedJob] = field(default_factory=list)
    detected_system: str = ""
    reason: str = ""
    platform: str = ""
    ats_identifier: str = ""
    ats_url: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def jobs_available(self) -> bool:
        return bool(self.postings)


def _has_job_postings(url: str, html: str) -> bool:
    """The resolver's early-exit test: this page carries real structured job
    data, so there is nothing to gain from crawling further."""
    return bool(parse_job_postings(html, url))


def probe_careers_page(website: str, *, fetch=polite_get) -> CareersProbeResult:
    """
    Find a company's real careers system on its OWN verified domain, and
    read whatever job data is legitimately there.

    The crawl itself belongs to careers_resolver.resolve_careers — homepage,
    the company's own careers links, careers subdomains, conventional paths,
    all robots-checked, rate-limited, and capped at a handful of requests.
    This function decides what the result MEANS, in four honest outcomes:

      1. A recognised ATS with a queryable identifier -> `platform` is set
         and the caller should ask that adapter for the real postings. Any
         JSON-LD on the page is still parsed and returned as a floor.
      2. A page with JobPosting JSON-LD -> real postings, returned directly.
      3. A reachable careers page with neither -> a verified company whose
         job data is not machine-readable. `reachable=True`, `postings`
         empty, and the caller must not read that as "confirmed zero".
      4. Nothing reachable at all -> not verified by this method.
    """
    resolution = resolve_careers(website, fetch=fetch, stop_when=_has_job_postings)
    result = CareersProbeResult(website=website, evidence=list(resolution.evidence))

    if not resolution.reachable:
        result.reason = "no careers/jobs page could be reached on this domain"
        return result

    result.reachable = True
    result.url = resolution.careers_url
    result.detected_system = resolution.ats_label
    result.ats_url = resolution.ats_url
    result.postings = parse_job_postings(resolution.careers_html, resolution.careers_url)

    if resolution.has_queryable_ats:
        result.platform = resolution.platform
        result.ats_identifier = resolution.identifier
        result.reason = f"{resolution.ats_label} careers system found at {resolution.identifier}"
        return result

    if result.postings:
        result.reason = f"{len(result.postings)} JobPosting record(s) found"
        return result

    system = f" ({result.detected_system})" if result.detected_system else ""
    result.reason = f"careers page reachable{system} but no machine-readable job data found"
    return result
