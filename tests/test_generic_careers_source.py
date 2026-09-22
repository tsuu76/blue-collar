"""
Tests for the ATS-agnostic careers-page source: JobPosting JSON-LD parsing,
robots.txt honoring, ATS-fingerprint detection, and the "reachable but
unreadable" outcome that must never be reported as "confirmed zero".

No network: every test injects a fake `fetch`. Every URL is `.invalid`.
"""
from __future__ import annotations

import pytest

from src.job_discovery.sources.generic_careers import (
    CareersProbeResult,
    GenericCareersSource,
    detect_known_system,
    parse_job_postings,
    probe_careers_page,
    robots_allows,
)


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")


@pytest.fixture(autouse=True)
def _clear_robots_cache():
    """
    _robots_cache is a per-process, per-host cache (same pattern as
    job_discovery.base's rate limiter) — real, deliberate, and fine in
    production. In tests it must be cleared between cases, or a robots.txt
    fetched by one test's fake fetcher would silently answer a later test.
    """
    from src.job_discovery.sources import generic_careers

    generic_careers._robots_cache.clear()
    yield
    generic_careers._robots_cache.clear()


class FakeFetcher:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls: list[str] = []

    def __call__(self, url, timeout=None, **kwargs):
        self.calls.append(url)
        if url not in self.pages:
            return FakeResponse(status_code=404)
        body, status = self.pages[url]
        return FakeResponse(body, status)


def _fetcher(pages: dict[str, str], *, robots: str | None = None) -> FakeFetcher:
    full = {url: (body, 200) for url, body in pages.items()}
    if robots is not None:
        pass  # robots URLs added by caller directly with a distinct key below
    return FakeFetcher(full)


SINGLE_JOBPOSTING = """
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Service Desk Analyst",
 "description": "<p>Troubleshoot issues daily.</p>",
 "url": "https://example.invalid/careers/1",
 "hiringOrganization": {"name": "Example Co"},
 "jobLocation": {"address": {"addressLocality": "Sydney", "addressRegion": "NSW"}}}
</script>
"""

GRAPH_JOBPOSTINGS = """
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebPage", "name": "Careers"},
  {"@type": "JobPosting", "title": "IT Support Officer", "url": "https://example.invalid/careers/2"},
  {"@type": ["JobPosting"], "title": "QA Analyst", "url": "https://example.invalid/careers/3"}
]}
</script>
"""

ARRAY_OF_POSTINGS = """
<script type="application/ld+json">
[{"@type": "JobPosting", "title": "Cloud Engineer", "url": "https://example.invalid/careers/4"},
 {"@type": "JobPosting", "title": "Network Technician", "url": "https://example.invalid/careers/5"}]
</script>
"""

MALFORMED_JSON = """
<script type="application/ld+json">
{ this is not valid json, }
</script>
"""

NO_TITLE_POSTING = """
<script type="application/ld+json">
{"@type": "JobPosting", "description": "No title given at all."}
</script>
"""

WORKDAY_WIDGET_NO_JSONLD = """
<html><body>
<h1>Careers</h1>
<iframe src="https://example.wd1.myworkdayjobs.com/en-US/External"></iframe>
</body></html>
"""

SOFT_404 = "<html><body><h1>Oops! That page could not be found</h1></body></html>"


class TestJsonLdParsing:
    def test_a_single_jobposting_object(self):
        jobs = parse_job_postings(SINGLE_JOBPOSTING, "https://example.invalid/careers")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Service Desk Analyst"
        assert job.url == "https://example.invalid/careers/1"
        assert job.company == "Example Co"
        assert job.location == "Sydney, NSW"
        assert "Troubleshoot issues daily." in job.description
        assert job.source == "generic_careers_jsonld"

    def test_an_at_graph_array_with_mixed_types(self):
        """Real sites often bundle a JobPosting alongside WebPage/Organization
        nodes in one @graph — only the JobPosting nodes should come out."""
        jobs = parse_job_postings(GRAPH_JOBPOSTINGS, "https://example.invalid/careers")
        titles = {j.title for j in jobs}
        assert titles == {"IT Support Officer", "QA Analyst"}

    def test_a_type_as_a_list_is_still_recognised(self):
        jobs = parse_job_postings(GRAPH_JOBPOSTINGS, "https://example.invalid/careers")
        assert any(j.title == "QA Analyst" for j in jobs)

    def test_a_bare_array_of_postings(self):
        jobs = parse_job_postings(ARRAY_OF_POSTINGS, "https://example.invalid/careers")
        assert {j.title for j in jobs} == {"Cloud Engineer", "Network Technician"}

    def test_malformed_json_is_skipped_not_raised(self):
        assert parse_job_postings(MALFORMED_JSON, "https://example.invalid/careers") == []

    def test_a_posting_with_no_title_is_not_fabricated(self):
        """No title is not enough evidence to call it a real posting — it
        must be skipped, not filled in with an invented value."""
        assert parse_job_postings(NO_TITLE_POSTING, "https://example.invalid/careers") == []

    def test_no_ld_json_at_all_yields_nothing(self):
        assert parse_job_postings(WORKDAY_WIDGET_NO_JSONLD, "https://example.invalid/careers") == []

    def test_url_falls_back_to_the_page_when_the_posting_has_none(self):
        html = '<script type="application/ld+json">{"@type": "JobPosting", "title": "Role"}</script>'
        jobs = parse_job_postings(html, "https://example.invalid/careers")
        assert jobs[0].url == "https://example.invalid/careers"

    def test_it_never_scrapes_third_party_domains(self):
        """The parser only ever reads the HTML it's handed — it makes no
        request of its own to any other host."""
        import inspect

        from src.job_discovery.sources import generic_careers
        source = inspect.getsource(generic_careers.parse_job_postings)
        assert "polite_get" not in source
        assert "requests." not in source


class TestKnownSystemDetection:
    def test_workday_widget_is_recognised(self):
        assert detect_known_system(WORKDAY_WIDGET_NO_JSONLD) == "Workday"

    def test_pageup_is_recognised(self):
        assert detect_known_system('<script src="https://acme.pageuphr.com/x"></script>') == "PageUp"

    def test_taleo_is_recognised(self):
        assert detect_known_system('<a href="https://acme.taleo.net/careersection">Jobs</a>') == "Taleo"

    def test_unrecognised_page_returns_empty(self):
        assert detect_known_system("<html><body>Nothing special here.</body></html>") == ""

    def test_detection_never_extracts_a_posting(self):
        """Recognising the system is informational only — it must never, by
        itself, produce a NormalizedJob."""
        assert parse_job_postings(WORKDAY_WIDGET_NO_JSONLD, "https://example.invalid") == []


class TestRobotsTxt:
    def test_disallowed_path_is_refused(self):
        fetch = FakeFetcher({
            "https://example.invalid/robots.txt": ("User-agent: *\nDisallow: /careers\n", 200),
        })
        assert robots_allows("https://example.invalid/careers", fetch=fetch) is False

    def test_allowed_path_is_permitted(self):
        fetch = FakeFetcher({
            "https://example.invalid/robots.txt": ("User-agent: *\nDisallow: /admin\n", 200),
        })
        assert robots_allows("https://example.invalid/careers", fetch=fetch) is True

    def test_missing_robots_txt_defaults_to_allowed(self):
        """No robots.txt at all is the standard convention for 'everything
        is allowed', not a reason to refuse."""
        fetch = FakeFetcher({})
        assert robots_allows("https://example.invalid/careers", fetch=fetch) is True

    def test_a_disallowed_page_is_never_fetched_by_the_prober(self):
        class RecordingFetcher:
            def __init__(self):
                self.fetched = []

            def __call__(self, url, timeout=None, **kwargs):
                if url.endswith("/robots.txt"):
                    return FakeResponse("User-agent: *\nDisallow: /\n", 200)
                self.fetched.append(url)
                return FakeResponse(SINGLE_JOBPOSTING, 200)

        fetch = RecordingFetcher()
        result = probe_careers_page("https://example.invalid", fetch=fetch)
        assert result.reachable is False
        assert fetch.fetched == []


class TestProbeCareersPage:
    def test_structured_data_found_returns_immediately(self):
        fetch = FakeFetcher({"https://example.invalid/careers": (SINGLE_JOBPOSTING, 200)})
        result = probe_careers_page("https://example.invalid", fetch=fetch)
        assert result.reachable is True
        assert result.jobs_available is True
        assert len(result.postings) == 1

    def test_a_workday_page_now_yields_a_queryable_identifier(self):
        """
        This used to be the "reachable but unreadable" case, and it is the
        one behaviour Stage 1 deliberately changes: the same Workday-
        embedding page now hands back the tenant/site the adapter needs, so
        the company can produce real postings instead of being parked.
        `jobs_available` stays False here because THIS page carries no
        structured data — the postings come from querying the ATS, which is
        the caller's next step, not the prober's.
        """
        fetch = FakeFetcher({"https://example.invalid/careers": (WORKDAY_WIDGET_NO_JSONLD, 200)})
        result = probe_careers_page("https://example.invalid", fetch=fetch)
        assert result.reachable is True
        assert result.jobs_available is False
        assert result.postings == []
        assert result.detected_system == "Workday"
        assert result.platform == "workday"
        assert result.ats_identifier == "https://example.wd1.myworkdayjobs.com/example/External"
        assert result.evidence  # how it got there is always recorded

    def test_reachable_but_unparseable_is_not_confirmed_zero(self):
        """
        The invariant that must survive Stage 1: a real careers page whose
        job data this project cannot read is still a verified, reachable
        result — `jobs_available` is False, but `reachable` is True, and
        nothing claims "no openings".
        """
        plain = "<html><body><h1>Careers</h1><p>Apply in person at any store.</p></body></html>"
        fetch = FakeFetcher({"https://example.invalid/careers": (plain, 200)})
        result = probe_careers_page("https://example.invalid", fetch=fetch)
        assert result.reachable is True
        assert result.jobs_available is False
        assert result.postings == []
        assert result.platform == ""
        assert "no machine-readable job data" in result.reason

    def test_a_soft_404_is_not_treated_as_reachable(self):
        fetch = FakeFetcher({"https://example.invalid/careers": (SOFT_404, 200)})
        result = probe_careers_page("https://example.invalid", fetch=fetch)
        # /careers looked like a soft-404; no other path had anything either.
        assert result.reachable is False

    def test_nothing_reachable_at_all(self):
        result = probe_careers_page("https://example.invalid", fetch=FakeFetcher({}))
        assert result.reachable is False
        assert result.postings == []

    def test_no_website_is_not_a_crash(self):
        assert probe_careers_page("", fetch=FakeFetcher({})).reachable is False

    def test_a_non_http_scheme_is_refused(self):
        result = probe_careers_page("javascript:alert(1)", fetch=FakeFetcher({}))
        assert result.reachable is False

    def test_only_the_verified_domain_is_ever_fetched(self):
        fetch = FakeFetcher({"https://example.invalid/careers": (SINGLE_JOBPOSTING, 200)})
        probe_careers_page("https://example.invalid", fetch=fetch)
        for url in fetch.calls:
            assert "example.invalid" in url

    def test_later_path_is_tried_when_an_earlier_one_is_a_soft_404(self):
        fetch = FakeFetcher({
            "https://example.invalid/careers": (SOFT_404, 200),
            "https://example.invalid/jobs": (SINGLE_JOBPOSTING, 200),
        })
        result = probe_careers_page("https://example.invalid", fetch=fetch)
        assert result.reachable is True
        assert result.jobs_available is True


class TestGenericCareersSourceAdapter:
    """The JobDiscoverySource interface used at pipeline run time — a single,
    already-known-good URL, re-fetched directly (no path guessing)."""

    def test_discover_reads_one_known_url(self, monkeypatch):
        source = GenericCareersSource()
        fetch_calls = []

        def fake_polite_get(url, timeout=None, **kwargs):
            fetch_calls.append(url)
            return FakeResponse(SINGLE_JOBPOSTING, 200)

        monkeypatch.setattr(
            "src.job_discovery.sources.generic_careers.polite_get", fake_polite_get
        )
        monkeypatch.setattr(
            "src.job_discovery.sources.generic_careers.robots_allows", lambda url, **kw: True
        )
        jobs = source.discover("https://example.invalid/careers")
        assert len(jobs) == 1
        assert fetch_calls == ["https://example.invalid/careers"]

    def test_empty_identifier_returns_nothing(self):
        assert GenericCareersSource().discover("") == []

    def test_robots_disallow_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "src.job_discovery.sources.generic_careers.robots_allows", lambda url, **kw: False
        )
        assert GenericCareersSource().discover("https://example.invalid/careers") == []

    def test_a_fetch_failure_propagates_like_other_adapters(self, monkeypatch):
        """
        research.py wraps every adapter.discover() call in a broad
        try/except — this adapter must raise on total failure, exactly like
        Greenhouse's resp.raise_for_status(), not swallow it silently.
        """
        class Boom:
            status_code = 500

            def raise_for_status(self):
                raise RuntimeError("500 Server Error")

        monkeypatch.setattr(
            "src.job_discovery.sources.generic_careers.polite_get", lambda url, **kw: Boom()
        )
        monkeypatch.setattr(
            "src.job_discovery.sources.generic_careers.robots_allows", lambda url, **kw: True
        )
        with pytest.raises(RuntimeError):
            GenericCareersSource().discover("https://example.invalid/careers")

    def test_it_never_invents_a_posting_when_nothing_structured_is_present(self, monkeypatch):
        monkeypatch.setattr(
            "src.job_discovery.sources.generic_careers.polite_get",
            lambda url, **kw: FakeResponse(WORKDAY_WIDGET_NO_JSONLD, 200),
        )
        monkeypatch.setattr(
            "src.job_discovery.sources.generic_careers.robots_allows", lambda url, **kw: True
        )
        assert GenericCareersSource().discover("https://example.invalid/careers") == []
