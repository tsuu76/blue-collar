"""
Crawlee-driven multi-page JSON-LD careers source.

The existing `GenericCareersSource` (`careers_page` platform) reads
schema.org `JobPosting` markup from ONE already-known careers URL — good
for a small employer whose whole listing fits on that page, useless for a
larger employer whose openings are paginated or split across a listing
page plus per-job detail pages.

This adapter fills that gap without introducing a second scraping model.
It walks a small, bounded set of pages under the employer's own host,
extracts JSON-LD `JobPosting` markup from each using the SAME parser
`GenericCareersSource` already uses (`parse_job_postings`), and returns
`NormalizedJob` objects that flow through the existing insert_job/dedupe
pipeline unchanged.

Configuration lives in `config/employers.json` under the platform key
`crawlee_jsonld`, and looks like:

    {
      "company": "Example",
      "platform": "crawlee_jsonld",
      "identifier": "{\\"seed\\":\\"https://example.com/careers\\",
                       \\"link_pattern\\":\\"/careers/(job|listing)\\"}",
      "enabled": true
    }

`identifier` is JSON so a single string field can carry a full spec, but
it is parsed and validated at THIS adapter's boundary (via
`_parse_identifier`) into a typed `CrawlSpec` before Crawlee sees it.
Malformed config raises `ValueError`, which the registry catches per
employer — one bad entry never stops discovery for the rest.

The emitted `NormalizedJob.source` is `f"crawl:{host}"` where `host` is
the seed URL's registrable host, NOT `"crawlee_jsonld"`. Two different
employers therefore live in two different source namespaces, which is
what the DB's `(source, source_job_id)` dedupe key relies on to avoid
collisions between unrelated postings. The mechanism used to fetch them
is recorded in the `CrawlStats` this adapter's caller can consume — it
is deliberately NOT stamped onto every job.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from src.sources.base import NormalizedJob

from ..base import JobDiscoverySource
from ..crawlee_runner import CrawlSpec, CrawlStats, run_crawl
from .generic_careers import parse_job_postings

logger = logging.getLogger("job_hunter.job_discovery.crawlee_jsonld")


@dataclass(frozen=True)
class CrawleeJsonLdConfig:
    """
    Parsed employer-config identifier. Kept typed and immutable so the
    parsing step at the adapter boundary either produces a valid config
    or raises — nothing partial leaks into the crawler.
    """

    seed_url: str
    link_pattern: re.Pattern[str] | None
    max_requests: int
    max_concurrency: int
    max_requests_per_minute: float


def _parse_identifier(identifier: str) -> CrawleeJsonLdConfig:
    """
    Parse an employer-config identifier into a typed config. Accepts:

      * A bare URL string, e.g. "https://example.com/careers" — used
        as-is, no link-pattern filter (small careers pages only).
      * A JSON object with:
          "seed" (required): the seed URL.
          "link_pattern" (optional): regex, matched against the full
              canonical URL of same-host links before enqueueing.
          "max_requests" / "max_concurrency" / "max_requests_per_minute"
              (optional): per-crawl overrides.

    Raises `ValueError` with a clear message on any malformed input.
    """

    raw = (identifier or "").strip()
    if not raw:
        raise ValueError("crawlee_jsonld: identifier is empty")

    if raw.startswith("http://") or raw.startswith("https://"):
        seed_url = raw
        payload: dict = {}
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"crawlee_jsonld: identifier is not a URL or JSON object: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("crawlee_jsonld: identifier JSON must be an object")
        seed_url = str(payload.get("seed") or "").strip()
        if not seed_url:
            raise ValueError("crawlee_jsonld: identifier JSON is missing required key 'seed'")
        if not (seed_url.startswith("http://") or seed_url.startswith("https://")):
            raise ValueError(f"crawlee_jsonld: seed URL must be absolute http(s): {seed_url!r}")

    pattern_source = payload.get("link_pattern")
    if pattern_source is not None and not isinstance(pattern_source, str):
        raise ValueError("crawlee_jsonld: link_pattern must be a string regex")
    try:
        pattern = re.compile(pattern_source) if pattern_source else None
    except re.error as exc:
        raise ValueError(f"crawlee_jsonld: link_pattern is not a valid regex: {exc}") from exc

    def _positive_int(key: str, default: int) -> int:
        value = payload.get(key, default)
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"crawlee_jsonld: {key} must be a positive integer")
        return value

    def _positive_float(key: str, default: float) -> float:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"crawlee_jsonld: {key} must be a positive number")
        return float(value)

    return CrawleeJsonLdConfig(
        seed_url=seed_url,
        link_pattern=pattern,
        max_requests=_positive_int("max_requests", 60),
        max_concurrency=_positive_int("max_concurrency", 2),
        max_requests_per_minute=_positive_float("max_requests_per_minute", 30.0),
    )


def _host_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _spec_from_config(cfg: CrawleeJsonLdConfig) -> CrawlSpec:
    host = urlparse(cfg.seed_url).netloc.lower()
    if not host:
        raise ValueError(f"crawlee_jsonld: seed URL has no host: {cfg.seed_url!r}")
    # Allow both bare and www-prefixed variants of the seed host — a link
    # off the listing may use either form and they point at the same site.
    hosts = {host, host[4:] if host.startswith("www.") else f"www.{host}"}
    return CrawlSpec(
        seed_urls=(cfg.seed_url,),
        allowed_hosts=frozenset(hosts),
        link_pattern=cfg.link_pattern,
        max_requests=cfg.max_requests,
        max_concurrency=cfg.max_concurrency,
        max_requests_per_minute=cfg.max_requests_per_minute,
    )


class CrawleeJsonLdSource(JobDiscoverySource):
    """
    Multi-page JSON-LD JobPosting adapter driven by Crawlee. Emits the
    same `NormalizedJob` shape as every other adapter — the mechanism is
    private to this module and to `crawlee_runner`.
    """

    platform = "crawlee_jsonld"

    def discover(self, identifier: str) -> list[NormalizedJob]:
        cfg = _parse_identifier(identifier)
        spec = _spec_from_config(cfg)
        pages, stats = run_crawl(spec)
        return list(_extract_jobs(pages, cfg.seed_url, stats))


def _extract_jobs(
    pages: dict[str, str], seed_url: str, stats: CrawlStats
) -> list[NormalizedJob]:
    """
    Walk every fetched page's HTML and pull JobPosting JSON-LD out of it
    via the existing extractor. The employer source label is derived from
    the seed URL's host — NOT from the crawler mechanism — so
    `(source, source_job_id)` remains unique per employer, which is what
    the DB dedupe hash requires.
    """
    employer_source = f"crawl:{_host_of(seed_url)}"
    jobs: list[NormalizedJob] = []
    seen_ids: set[str] = set()
    for page_url, html in pages.items():
        try:
            raw_jobs = parse_job_postings(html, page_url)
        except Exception as exc:  # noqa: BLE001 — one bad page must not stop the rest
            logger.warning("Failed to parse JSON-LD from %s: %s", page_url, exc)
            stats.failed += 1
            continue
        for job in raw_jobs:
            # Rebind `source` to the employer namespace; the generic
            # extractor sets it to its own label, which would collapse
            # every crawled employer into one source and break dedupe.
            key = (job.source_job_id or job.url).strip()
            if key in seen_ids:
                continue
            seen_ids.add(key)
            jobs.append(
                NormalizedJob(
                    source=employer_source,
                    url=job.url,
                    title=job.title,
                    description=job.description,
                    company=job.company,
                    location=job.location,
                    salary=job.salary,
                    source_job_id=job.source_job_id,
                )
            )
    return jobs
