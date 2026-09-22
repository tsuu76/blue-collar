"""
Crawlee-based crawl harness for job discovery.

This module is the ONE place that knows how to drive Crawlee. Every source
adapter that needs to walk multiple pages of a careers site (listing pages,
pagination links, detail pages) declares a `CrawlSpec` and hands it to
`run_crawl` — the adapter never touches Crawlee's own types directly. That
keeps the Crawlee upgrade surface small: if the library's API shifts, one
file changes, not every adapter.

Design constraints enforced here (see the project's adapter design rules):

  * The `source` field of a NormalizedJob names the underlying employer/site,
    not the crawler mechanism. This runner does NOT set `source` for the
    adapter — it only returns raw (canonical_url, html) pairs plus a
    per-crawl `CrawlStats` object. Populating `source` is the adapter's job,
    with knowledge of which employer this crawl was for.
  * URL canonicalization goes through `src.database.models.canonicalize_url`
    verbatim — the seen-set keyed by canonical URL is the same shape the DB
    dedupe hash uses, so a URL that the DB would treat as a duplicate is a
    URL this runner will not re-fetch either.
  * Every request is robots.txt-checked using the same `robots_allows` the
    other adapters use, before the URL is enqueued. Crawlee's own
    `respect_robots_txt_file` is left on as a second line of defence.
  * The crawl is confined to the seed URL's host. A link off-host is
    dropped, not followed — a job discovery crawl must never spider the
    open web.

The runner is deliberately synchronous at its public boundary (`run_crawl`)
so it fits the existing `JobDiscoverySource.discover(identifier: str) ->
list[NormalizedJob]` shape unchanged. Internally it drives Crawlee's async
API via `asyncio.run`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

from crawlee import ConcurrencySettings, service_locator
from crawlee.crawlers import ParselCrawler, ParselCrawlingContext
from crawlee.storage_clients import MemoryStorageClient

from src.database.models import canonicalize_url

from .careers_resolver import robots_allows

logger = logging.getLogger("job_hunter.job_discovery.crawlee")

# The Crawlee version this runner was written against. Recorded in
# CrawlStats so the DB row for a crawl always knows which mechanism
# produced it — the mechanism does NOT live on individual NormalizedJobs.
CRAWLER_ID = "crawlee_http_1.10.0"

# Belt-and-braces caps so a misconfigured spec can never turn into a
# runaway crawl. Real per-crawl values come from CrawlSpec.
DEFAULT_MAX_REQUESTS = 100
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MAX_REQUESTS_PER_MINUTE = 30.0  # 2s/host, matches polite_get


@dataclass(frozen=True)
class CrawlSpec:
    """
    Everything one crawl needs, parsed and validated by an adapter at its
    boundary. `link_pattern` limits which same-host links this crawl will
    enqueue from a listing page — a compiled regex against the URL, matched
    against the FULL canonicalized URL. Missing pattern means "enqueue any
    same-host link", which is only sensible for very small sites.
    """

    seed_urls: tuple[str, ...]
    allowed_hosts: frozenset[str]
    link_pattern: re.Pattern[str] | None = None
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_requests_per_minute: float = DEFAULT_MAX_REQUESTS_PER_MINUTE
    request_timeout_seconds: float = 15.0

    def is_allowed_host(self, url: str) -> bool:
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            return False
        return host in self.allowed_hosts

    def matches_link_pattern(self, url: str) -> bool:
        if self.link_pattern is None:
            return True
        return bool(self.link_pattern.search(url))


@dataclass
class CrawlStats:
    """
    Per-crawl counters — properties of the run, not of individual jobs. The
    `crawler` field records the mechanism (`crawlee_http_1.10.0`) so the
    adapter/caller can log or persist which crawler produced the postings
    without stamping that fact onto every job's `discovery_metadata_json`.
    """

    crawler: str = CRAWLER_ID
    discovered: int = 0        # unique canonical URLs the crawl saw at all
    fetched: int = 0            # pages actually loaded
    skipped_off_host: int = 0
    skipped_robots: int = 0
    skipped_pattern: int = 0
    failed: int = 0
    seed_urls: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    robots_denied_urls: list[str] = field(default_factory=list)


async def _run_crawl_async(
    spec: CrawlSpec,
    pages: dict[str, str],
    stats: CrawlStats,
) -> None:
    """
    Drive one Crawlee run to completion. Results are collected into
    `pages` (canonical URL -> HTML) and `stats` in place, so the sync
    wrapper below can return them without needing to plumb a return
    value back through `asyncio.run`.
    """

    # Crawlee's storage_instance_manager caches RequestQueue/Dataset instances
    # process-wide, keyed by (storage_type, alias). If we don't clear it
    # between runs, a second `run_crawl` reopens the previous run's already-
    # drained "default" RequestQueue and does nothing. A fresh
    # MemoryStorageClient is not enough — the CACHE still hands back the
    # old queue backed by whichever client made it first.
    service_locator.storage_instance_manager.clear_cache()

    crawler = ParselCrawler(
        max_requests_per_crawl=spec.max_requests,
        concurrency_settings=ConcurrencySettings(
            max_concurrency=spec.max_concurrency,
            # desired_concurrency defaults to 10 in Crawlee 1.10, and
            # Crawlee validates desired_concurrency <= max_concurrency —
            # so a low per-crawl cap needs desired lowered to match, or
            # it raises before the crawl even starts.
            desired_concurrency=min(spec.max_concurrency, 10),
            max_tasks_per_minute=spec.max_requests_per_minute,
        ),
        request_handler_timeout=timedelta(seconds=spec.request_timeout_seconds),
        # Crawlee's own robots checker as a second line of defence — the
        # primary robots gate is `robots_allows` on every enqueue below,
        # to keep behaviour consistent with the project's other adapters.
        respect_robots_txt_file=True,
        # In-memory storage: nothing about a job-discovery run needs to
        # survive across process boundaries, and the on-disk default would
        # otherwise leave a `storage/` directory in the working directory.
        storage_client=MemoryStorageClient(),
        configure_logging=False,
    )

    @crawler.router.default_handler
    async def handle(context: ParselCrawlingContext) -> None:
        canonical = canonicalize_url(context.request.url)
        # `context.http_response` is the raw response; `.read()` returns bytes.
        # `context.selector.get()` gives the decoded HTML string parsel already
        # parsed for us, which is what the JSON-LD extractor wants.
        html = context.selector.get() or ""
        pages[canonical] = html
        stats.fetched += 1

        # Enqueue same-host links matching the adapter's pattern. Every
        # enqueue is canonicalized and robots-checked BEFORE it reaches
        # Crawlee's own queue, so the seen-set stays consistent with the
        # DB's dedupe view of URL identity.
        for link in context.selector.css("a::attr(href)").getall():
            absolute = _absolutize(context.request.url, link)
            if not absolute:
                continue
            canonical_link = canonicalize_url(absolute)
            if not canonical_link or canonical_link in pages:
                continue
            if not spec.is_allowed_host(canonical_link):
                stats.skipped_off_host += 1
                continue
            if not spec.matches_link_pattern(canonical_link):
                stats.skipped_pattern += 1
                continue
            if not robots_allows(canonical_link):
                stats.skipped_robots += 1
                stats.robots_denied_urls.append(canonical_link)
                continue
            stats.discovered += 1
            await context.add_requests([canonical_link])

    # Robots-check the seeds too — Crawlee will happily fetch a seed
    # even if the site later denies its own directory, so we gate here.
    starting: list[str] = []
    for seed in spec.seed_urls:
        canonical_seed = canonicalize_url(seed)
        if not canonical_seed:
            continue
        if not spec.is_allowed_host(canonical_seed):
            stats.skipped_off_host += 1
            continue
        if not robots_allows(canonical_seed):
            stats.skipped_robots += 1
            stats.robots_denied_urls.append(canonical_seed)
            continue
        stats.discovered += 1
        starting.append(canonical_seed)

    if not starting:
        return

    final = await crawler.run(starting)
    stats.failed += final.requests_failed
    stats.duration_seconds = final.crawler_runtime.total_seconds()


def _absolutize(base: str, href: str) -> str:
    """
    Small, dependency-free URL joiner. Parsel exposes a helper for this
    but its shape has drifted between versions — the stdlib is stable and
    the URLs we need to join are ordinary careers-site anchor hrefs.
    """
    if not href:
        return ""
    href = href.strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    from urllib.parse import urljoin

    return urljoin(base, href)


def run_crawl(spec: CrawlSpec) -> tuple[dict[str, str], CrawlStats]:
    """
    Run one Crawlee crawl, blocking. Returns a dict mapping the canonical
    URL of every successfully fetched page to its HTML body, plus a
    per-crawl CrawlStats.

    Never raises for a single-page failure or a network hiccup — those
    are counted in `stats.failed` and the crawl continues with whatever
    else was in the queue. A completely broken spec (no seeds, all seeds
    off-host or robots-denied) returns an empty dict and stats with
    fetched=0, which the caller must be able to handle without treating
    it as "confirmed zero postings".
    """
    stats = CrawlStats(seed_urls=tuple(spec.seed_urls))
    pages: dict[str, str] = {}

    try:
        asyncio.run(_run_crawl_async(spec, pages, stats))
    except Exception as exc:  # noqa: BLE001 — one crawl's total failure must not stop the rest
        logger.warning("Crawlee crawl failed for seeds %s: %s", spec.seed_urls, exc)
        stats.failed += 1

    logger.info(
        "[CRAWLEE] seeds=%d discovered=%d fetched=%d off_host=%d "
        "robots=%d pattern=%d failed=%d duration=%.2fs",
        len(spec.seed_urls),
        stats.discovered,
        stats.fetched,
        stats.skipped_off_host,
        stats.skipped_robots,
        stats.skipped_pattern,
        stats.failed,
        stats.duration_seconds,
    )

    return pages, stats
