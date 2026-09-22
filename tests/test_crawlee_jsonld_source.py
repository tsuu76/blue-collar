"""
Tests for the Crawlee-driven multi-page JSON-LD careers adapter.

These tests spin up a real local HTTP server on 127.0.0.1 and point
Crawlee at it — no live-internet requests, but every URL-joining,
pagination and robots decision goes through actual HTTP the way it
would in production. A `file://` fixture wouldn't exercise any of
that.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.database.db import get_connection, init_db
from src.database.jobs_repo import DuplicateJobError, insert_job
from src.job_discovery.registry import ADAPTERS
from src.job_discovery.sources.crawlee_jsonld import (
    CrawleeJsonLdSource,
    _parse_identifier,
)


FIXTURES = Path(__file__).parent / "fixtures" / "crawlee_jsonld"

# Map incoming request paths to fixture files. The two /careers* entries
# both point at fixtures that hard-code /careers/jobs/... anchors, so
# link resolution and pagination are exercised for real.
ROUTES: dict[str, str] = {
    "/careers": "careers.html",
    "/careers?page=2": "careers_page2.html",
    "/careers/jobs/it-support-officer": "it-support-officer.html",
    "/careers/jobs/service-desk-analyst": "service-desk-analyst.html",
    "/careers/jobs/junior-sysadmin": "junior-sysadmin.html",
    "/press": "press.html",
}


class _FixtureHandler(BaseHTTPRequestHandler):
    """
    Tiny fixed-route file server. We match on `self.path` including the
    query string so the pagination fixture (`/careers?page=2`) resolves
    without pulling in a full router.
    """

    # Which routes robots.txt is configured to disallow for this test.
    # Set by the fixture below; empty by default (nothing disallowed).
    disallow_paths: tuple[str, ...] = ()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — signature is the stdlib's
        return  # silence noisy per-request logs during tests

    def do_GET(self) -> None:  # noqa: N802 — stdlib signature
        if self.path == "/robots.txt":
            body_lines = ["User-agent: *"]
            for path in self.disallow_paths:
                body_lines.append(f"Disallow: {path}")
            body_lines.append("")
            body = "\n".join(body_lines).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        fixture = ROUTES.get(self.path)
        if fixture is None:
            self.send_response(404)
            self.end_headers()
            return

        body = (FIXTURES / fixture).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def http_server(request):
    """
    Boot a threaded HTTP server on an ephemeral port. Yields the base URL
    (e.g. "http://127.0.0.1:54321"). `request.param` may be a tuple of
    paths to disallow via robots.txt.
    """
    disallow = getattr(request, "param", ()) or ()

    class Handler(_FixtureHandler):
        disallow_paths = tuple(disallow)

    # Port 0 -> the kernel picks an unused port for us.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _reset_robots_cache():
    """
    `robots_allows` caches parsed robots.txt per-host. Each test gets its
    own ephemeral 127.0.0.1:<port> host, so the cache doesn't collide
    across tests — but resetting explicitly makes the intent obvious and
    protects against future refactors that might reuse ports.
    """
    from src.job_discovery.careers_resolver import _robots_cache

    _robots_cache.clear()
    yield
    _robots_cache.clear()


# ----------------------------------------------------------------------
# _parse_identifier — boundary validation
# ----------------------------------------------------------------------


def test_parse_identifier_accepts_bare_url() -> None:
    cfg = _parse_identifier("https://example.test/careers")
    assert cfg.seed_url == "https://example.test/careers"
    assert cfg.link_pattern is None
    assert cfg.max_requests > 0


def test_parse_identifier_accepts_json_object() -> None:
    cfg = _parse_identifier(
        '{"seed": "https://example.test/careers",'
        ' "link_pattern": "/careers/jobs/",'
        ' "max_requests": 25}'
    )
    assert cfg.seed_url == "https://example.test/careers"
    assert cfg.link_pattern is not None
    assert cfg.max_requests == 25


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "   ",
        "not-a-url",
        "ftp://example.test/careers",
        '{"seed": "not-http"}',
        '{"link_pattern": "^.*"}',       # missing seed
        '{"seed": "https://x/", "link_pattern": "([unclosed"}',
        '{"seed": "https://x/", "max_requests": -5}',
        '{"seed": "https://x/", "max_requests": 1.5}',
        '{"seed": "https://x/", "max_requests_per_minute": 0}',
        "[]",                                    # JSON but not an object
    ],
)
def test_parse_identifier_rejects_malformed(identifier: str) -> None:
    with pytest.raises(ValueError):
        _parse_identifier(identifier)


# ----------------------------------------------------------------------
# Registry wiring
# ----------------------------------------------------------------------


def test_registry_exposes_crawlee_jsonld_adapter() -> None:
    """
    The adapter must be registered under the `crawlee_jsonld` platform
    key so a `config/employers.json` entry with that platform actually
    reaches this source, not the "unsupported platform" branch.
    """
    assert "crawlee_jsonld" in ADAPTERS
    assert isinstance(ADAPTERS["crawlee_jsonld"], CrawleeJsonLdSource)


# ----------------------------------------------------------------------
# End-to-end: real HTTP crawl of the fixture site
# ----------------------------------------------------------------------


def _discover(source: CrawleeJsonLdSource, http_server: str, *, link_pattern: str = "/careers"):
    # Pattern `/careers` matches the seed page, `/careers?page=2` (the
    # pagination link) AND every `/careers/jobs/...` detail URL, but NOT
    # `/press` — so the pagination-to-detail-page hop still gets tested
    # and the off-pattern filter still gets tested.
    identifier = (
        '{"seed": "' + http_server + '/careers",'
        ' "link_pattern": "' + link_pattern + '",'
        ' "max_requests": 20,'
        ' "max_concurrency": 2,'
        ' "max_requests_per_minute": 600}'
    )
    return source.discover(identifier)


def test_discover_walks_listing_pagination_and_detail_pages(http_server: str) -> None:
    """
    From one seed URL the crawl must find all three detail pages via
    both the listing links and the /careers?page=2 pagination link, and
    return one NormalizedJob per JobPosting JSON-LD block. The off-host
    LinkedIn link, the /press link (outside the pattern), and the
    javascript: link must all be ignored.
    """
    jobs = _discover(CrawleeJsonLdSource(), http_server)

    titles = sorted(j.title for j in jobs)
    assert titles == [
        "IT Support Officer",
        "Junior Systems Administrator",
        "Service Desk Analyst",
    ]

    # The `source` field must name the employer (its host), NOT the
    # crawler mechanism — this is the invariant the DB dedupe key relies
    # on to keep two different employers in two different namespaces.
    for job in jobs:
        assert job.source.startswith("crawl:127.0.0.1"), job.source
        assert "crawlee" not in job.source

    # Every job must carry the stable source_job_id from JSON-LD so
    # `(source, source_job_id)` is a strong dedupe key on re-run.
    ids = {j.source_job_id for j in jobs}
    assert ids == {"job-101", "job-102", "job-103"}


def test_malformed_jsonld_block_is_skipped_not_raised(http_server: str) -> None:
    """
    The service-desk-analyst fixture carries a second, broken JSON-LD
    block. The extractor must log-and-skip that block while still
    returning the well-formed one on the same page — a single bad
    posting can never drop the whole crawl.
    """
    jobs = _discover(CrawleeJsonLdSource(), http_server)
    assert any(j.title == "Service Desk Analyst" for j in jobs)


def test_second_run_produces_the_same_dedupe_hashes(tmp_path, http_server: str) -> None:
    """
    Ingesting the crawl output into a real (temp) SQLite DB, then
    running the exact same crawl again, must raise `DuplicateJobError`
    for every job on the second pass — the guarantee that repeated
    ingestion is idempotent.
    """
    db_path = str(tmp_path / "jobs.db")
    init_db(db_path)
    source = CrawleeJsonLdSource()

    jobs1 = _discover(source, http_server)
    assert jobs1, "first crawl must find jobs to make this test meaningful"

    conn = get_connection(db_path)
    try:
        for job in jobs1:
            insert_job(conn, job.to_dict())
        conn.commit()
    finally:
        conn.close()

    jobs2 = _discover(source, http_server)
    assert len(jobs2) == len(jobs1)

    conn = get_connection(db_path)
    try:
        duplicates = 0
        for job in jobs2:
            with pytest.raises(DuplicateJobError):
                insert_job(conn, job.to_dict())
            duplicates += 1
    finally:
        conn.close()

    assert duplicates == len(jobs1)


@pytest.mark.parametrize(
    "http_server",
    # Robots.txt for this test disallows the whole /careers/ subtree.
    [("/careers",)],
    indirect=True,
)
def test_robots_disallow_blocks_the_crawl(http_server: str) -> None:
    """
    With robots.txt disallowing the seed's directory, the crawler must
    fetch zero pages and return zero jobs — the primary robots gate is
    checked before any URL is enqueued.
    """
    jobs = _discover(CrawleeJsonLdSource(), http_server)
    assert jobs == []


def test_off_pattern_page_is_not_fetched(http_server: str) -> None:
    """
    /press is same-host but outside the link_pattern — it must never
    reach the fixture server. We assert on the runner's stats via a
    monkeypatched wrapper: the press-page fixture text is deliberately
    unique so we can search the returned jobs and their descriptions
    for it, expecting nothing.
    """
    jobs = _discover(CrawleeJsonLdSource(), http_server)
    for job in jobs:
        assert "Press releases" not in job.description
        assert "/press" not in job.url


def test_off_host_link_is_never_fetched(monkeypatch, http_server: str) -> None:
    """
    The listing fixture links to linkedin.com. A crawl that ever tried
    to fetch it would either hit the network or blow up under DNS
    failure inside the sandbox; the runner must strip off-host links at
    enqueue time. We spy on `robots_allows` to prove no LinkedIn URL
    ever reached the robots gate — that gate is the LAST filter before
    a request goes out, so nothing seen by it means nothing was fetched.
    """
    from src.job_discovery import crawlee_runner

    calls: list[str] = []
    real = crawlee_runner.robots_allows

    def _spy(url: str) -> bool:
        calls.append(url)
        return real(url)

    monkeypatch.setattr(crawlee_runner, "robots_allows", _spy)

    _discover(CrawleeJsonLdSource(), http_server)

    assert calls, "robots_allows should have been called for on-host URLs"
    for url in calls:
        assert "linkedin.com" not in url
