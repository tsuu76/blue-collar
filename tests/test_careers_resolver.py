"""
Tests for the bounded careers-page crawl and ATS fingerprinting.

No network anywhere: every test injects a fake `fetch` and every hostname is
`.invalid`, which is reserved by RFC 2606 and can never resolve. The fake
ATS responses are hand-written to the shapes these platforms really return.

The property under test throughout is narrower than "does it find a careers
page": it is that every identifier the resolver produces was READ OUT OF a
URL the company's own HTML actually contained, and that a company whose site
says nothing about an ATS gets no ATS attributed to it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.job_discovery.careers_resolver import (
    MAX_PAGE_FETCHES,
    CareersResolution,
    detect_ats_in_html,
    find_careers_links,
    registrable_domain,
    resolve_careers,
    robots_allows,
)


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


class FakeFetcher:
    """Serves a fixed URL->body map; anything else 404s, like a real site."""

    def __init__(self, pages: dict[str, str], *, robots: dict[str, str] | None = None):
        self.pages = dict(pages)
        self.robots = robots or {}
        self.calls: list[str] = []

    def __call__(self, url, timeout=None, **kwargs):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            body = self.robots.get(url)
            return FakeResponse(body, 200) if body is not None else FakeResponse("", 404)
        if url in self.pages:
            return FakeResponse(self.pages[url], 200)
        return FakeResponse("", 404)

    @property
    def page_calls(self) -> list[str]:
        return [url for url in self.calls if not url.endswith("/robots.txt")]


@pytest.fixture(autouse=True)
def _clear_robots_cache():
    from src.job_discovery import careers_resolver

    careers_resolver._robots_cache.clear()
    yield
    careers_resolver._robots_cache.clear()


HOMEPAGE_WITH_CAREERS_LINK = """
<html><body>
  <nav><a href="/about">About us</a><a href="/products">Products</a></nav>
  <footer><a href="/company/work-with-us">Work with us</a></footer>
</body></html>
"""

CAREERS_LANDING_LINKING_TO_ATS = """
<html><body>
  <h1>Working here</h1>
  <p>We hire across the country.</p>
  <a href="https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers">Search current vacancies</a>
</body></html>
"""


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

class TestAtsDetection:
    def test_workday_iframe_yields_tenant_and_site(self):
        html = '<iframe src="https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers"></iframe>'
        match = detect_ats_in_html(html, "https://acme.invalid/careers")
        assert match.label == "Workday"
        assert match.platform == "workday"
        assert match.identifier == "https://acme.wd3.myworkdayjobs.com/acme/Acme_Careers"

    def test_workday_cxs_endpoint_in_inline_script_is_recognised(self):
        html = """<script>var api = "https://acme.wd5.myworkday.com/wday/cxs/acme/External/jobs";</script>"""
        match = detect_ats_in_html(html, "https://acme.invalid/careers")
        assert match.platform == "workday"
        assert match.identifier == "https://acme.wd5.myworkday.com/acme/External"

    def test_pageup_is_named_but_not_queryable(self):
        """PageUp is recognised so the report can say so — but with no
        verified public endpoint it gets no platform, so nothing downstream
        can try to query it on a guess."""
        html = '<a href="https://acme.pageuppeople.com/search/en/1">Current vacancies</a>'
        match = detect_ats_in_html(html, "https://acme.invalid/careers")
        assert match.label == "PageUp"
        assert match.platform == ""

    def test_successfactors_is_named_but_not_queryable(self):
        html = '<script src="https://career5.successfactors.eu/career?company=acme"></script>'
        match = detect_ats_in_html(html, "https://acme.invalid/careers")
        assert match.label == "SuccessFactors"
        assert match.platform == ""

    def test_taleo_and_jobadder_are_named(self):
        assert detect_ats_in_html('<a href="https://acme.taleo.net/careersection/x">Jobs</a>', "").label == "Taleo"
        assert detect_ats_in_html('<a href="https://acme.jobadder.com/careers">Jobs</a>', "").label == "JobAdder"

    def test_a_discovered_greenhouse_slug_beats_a_named_only_system(self):
        """A page can mention an old Taleo site and an embedded Greenhouse
        board. The one this project can actually query wins."""
        html = (
            '<a href="https://acme.taleo.net/careersection/x">Old site</a>'
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmecorp"></script>'
        )
        match = detect_ats_in_html(html, "https://acme.invalid/careers")
        assert match.platform == "greenhouse"
        assert match.identifier == "acmecorp"

    def test_an_ordinary_page_gets_no_ats_attributed_to_it(self):
        html = "<html><body><h1>Careers</h1><p>Email us your CV.</p></body></html>"
        assert detect_ats_in_html(html, "https://acme.invalid/careers") is None

    def test_a_company_name_never_becomes_a_tenant(self):
        """The core safety property: a page that never mentions Workday
        cannot produce a Workday identifier, however obvious the guess."""
        html = "<html><body>Woolworths Group careers. We use Workday internally.</body></html>"
        assert detect_ats_in_html(html, "https://woolworths.invalid/careers") is None


class TestRegistrableDomain:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("www.acme.com.au", "acme.com.au"),
            ("careers.acme.com.au", "acme.com.au"),
            ("jobs.acme.com", "acme.com"),
            ("acme.invalid", "acme.invalid"),
            ("careers.dept.gov.au", "dept.gov.au"),
        ],
    )
    def test_organisation_level_domain(self, host, expected):
        assert registrable_domain(host) == expected


class TestLinkDiscovery:
    def test_finds_a_careers_link_by_its_text(self):
        links = find_careers_links(
            HOMEPAGE_WITH_CAREERS_LINK, "https://acme.invalid/", own_domain="acme.invalid"
        )
        assert links == ["https://acme.invalid/company/work-with-us"]

    def test_third_party_links_are_never_followed(self):
        html = (
            '<a href="https://www.linkedin.com/company/acme/jobs">Jobs</a>'
            '<a href="https://au.indeed.com/cmp/Acme/jobs">Careers</a>'
            '<a href="/careers">Careers</a>'
        )
        links = find_careers_links(html, "https://acme.invalid/", own_domain="acme.invalid")
        assert links == ["https://acme.invalid/careers"]

    def test_an_ats_link_outranks_an_in_site_one(self):
        html = (
            '<a href="/about/careers">Careers</a>'
            '<a href="https://acme.wd3.myworkdayjobs.com/Acme">Search jobs</a>'
        )
        links = find_careers_links(html, "https://acme.invalid/", own_domain="acme.invalid")
        assert links[0].startswith("https://acme.wd3.myworkdayjobs.com/")

    def test_non_careers_links_are_ignored(self):
        html = '<a href="/products">Our products</a><a href="/news">Newsroom</a>'
        assert find_careers_links(html, "https://acme.invalid/", own_domain="acme.invalid") == []


# --------------------------------------------------------------------------
# The crawl
# --------------------------------------------------------------------------

class TestResolveCareers:
    def test_homepage_to_careers_link_to_ats(self):
        """The whole point of this module: a company whose careers page is
        at no conventional path is still resolved, by following its own
        links — homepage -> "Work with us" -> Workday."""
        fetch = FakeFetcher({
            "https://acme.invalid/": HOMEPAGE_WITH_CAREERS_LINK,
            "https://acme.invalid/company/work-with-us": CAREERS_LANDING_LINKING_TO_ATS,
        })
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert resolution.platform == "workday"
        assert resolution.identifier == "https://acme.wd3.myworkdayjobs.com/acme/Acme_Careers"
        assert resolution.careers_url == "https://acme.invalid/company/work-with-us"

    def test_the_evidence_trail_names_every_hop(self):
        fetch = FakeFetcher({
            "https://acme.invalid/": HOMEPAGE_WITH_CAREERS_LINK,
            "https://acme.invalid/company/work-with-us": CAREERS_LANDING_LINKING_TO_ATS,
        })
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        trail = " | ".join(resolution.evidence)
        assert "https://acme.invalid/company/work-with-us" in trail
        assert "Workday" in trail

    def test_careers_subdomain_is_found_when_the_homepage_links_nowhere(self):
        fetch = FakeFetcher({
            "https://acme.invalid/": "<html><body>Just a brochure site.</body></html>",
            "https://careers.acme.invalid/": CAREERS_LANDING_LINKING_TO_ATS,
        })
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert resolution.careers_url == "https://careers.acme.invalid/"
        assert resolution.platform == "workday"

    def test_a_jobs_subdomain_is_also_tried(self):
        fetch = FakeFetcher({
            "https://acme.invalid/": "<html><body>Brochure.</body></html>",
            "https://jobs.acme.invalid/": "<html><body><h1>Job search</h1></body></html>",
        })
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert resolution.careers_url == "https://jobs.acme.invalid/"

    def test_a_dead_careers_link_does_not_end_the_crawl(self):
        """A link that 404s is skipped and the conventional paths still get
        their turn — one broken link must not lose the company."""
        html = '<html><body><a href="/careers-old">Careers</a></body></html>'
        fetch = FakeFetcher({
            "https://acme.invalid/": html,
            "https://acme.invalid/jobs": CAREERS_LANDING_LINKING_TO_ATS,
        })
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert "https://acme.invalid/careers-old" in fetch.page_calls
        assert resolution.careers_url == "https://acme.invalid/jobs"

    def test_an_unreachable_site_resolves_to_nothing(self):
        resolution = resolve_careers("https://acme.invalid", fetch=FakeFetcher({}))
        assert resolution.reachable is False
        assert resolution.platform == ""
        assert resolution.identifier == ""

    def test_an_invalid_website_is_not_a_crash(self):
        assert resolve_careers("", fetch=FakeFetcher({})).reachable is False
        assert resolve_careers("javascript:alert(1)", fetch=FakeFetcher({})).reachable is False
        assert resolve_careers("mailto:jobs@acme.invalid", fetch=FakeFetcher({})).reachable is False

    def test_a_reachable_page_with_no_ats_is_still_a_result(self):
        """The invariant that keeps this honest: no ATS found means the page
        is reported as-is, not discarded and not filled in with a guess."""
        fetch = FakeFetcher({
            "https://acme.invalid/careers": "<html><body><h1>Careers</h1>Apply in store.</body></html>",
        })
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert resolution.reachable is True
        assert resolution.careers_url == "https://acme.invalid/careers"
        assert resolution.platform == ""
        assert resolution.has_queryable_ats is False


class TestCrawlBudget:
    def test_never_exceeds_the_page_budget(self):
        """A site that links careers pages to careers pages forever still
        costs a fixed number of requests."""
        endless = (
            '<html><body><a href="/careers/1">Careers</a><a href="/careers/2">Jobs</a>'
            '<a href="/careers/3">Vacancies</a></body></html>'
        )
        fetch = FakeFetcher({f"https://acme.invalid/careers/{n}": endless for n in range(1, 40)}
                            | {"https://acme.invalid/": endless})
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert len(fetch.page_calls) <= MAX_PAGE_FETCHES
        assert resolution.fetches <= MAX_PAGE_FETCHES

    def test_the_budget_is_configurable_downwards(self):
        endless = '<html><body><a href="/careers/1">Careers</a></body></html>'
        fetch = FakeFetcher({"https://acme.invalid/": endless, "https://acme.invalid/careers/1": endless})
        resolve_careers("https://acme.invalid", fetch=fetch, max_fetches=2)
        assert len(fetch.page_calls) <= 2

    def test_finding_an_ats_stops_the_crawl_immediately(self):
        fetch = FakeFetcher({"https://acme.invalid/": CAREERS_LANDING_LINKING_TO_ATS})
        resolve_careers("https://acme.invalid", fetch=fetch)
        assert fetch.page_calls == ["https://acme.invalid/"]

    def test_stop_when_ends_the_crawl_early(self):
        """The hook generic_careers.py uses to stop on structured job data."""
        fetch = FakeFetcher({
            "https://acme.invalid/": "<html><body>Brochure</body></html>",
            "https://acme.invalid/careers": "<html><body>THE JOBS</body></html>",
        })
        resolve_careers(
            "https://acme.invalid", fetch=fetch, stop_when=lambda url, html: "THE JOBS" in html
        )
        assert fetch.page_calls == ["https://acme.invalid/", "https://acme.invalid/careers"]


class TestRobots:
    def test_a_disallowed_host_is_never_fetched(self):
        """robots.txt is scoped to the host that published it: a blanket
        Disallow on acme.invalid stops every page there, while a separate
        careers host answers for itself (see test_robots_is_consulted_per_host)."""
        fetch = FakeFetcher(
            {"https://acme.invalid/careers": CAREERS_LANDING_LINKING_TO_ATS},
            robots={"https://acme.invalid/robots.txt": "User-agent: *\nDisallow: /\n"},
        )
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert [url for url in fetch.page_calls if "//acme.invalid" in url] == []
        assert resolution.reachable is False

    def test_a_disallowed_path_is_skipped_but_others_are_tried(self):
        fetch = FakeFetcher(
            {
                "https://acme.invalid/": HOMEPAGE_WITH_CAREERS_LINK,
                "https://acme.invalid/company/work-with-us": CAREERS_LANDING_LINKING_TO_ATS,
            },
            robots={"https://acme.invalid/robots.txt": "User-agent: *\nDisallow: /company/\n"},
        )
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert "https://acme.invalid/company/work-with-us" not in fetch.page_calls
        assert resolution.platform == ""
        assert any("robots.txt disallows" in line for line in resolution.evidence)

    def test_a_missing_robots_txt_allows_the_crawl(self):
        fetch = FakeFetcher({"https://acme.invalid/careers": CAREERS_LANDING_LINKING_TO_ATS})
        assert resolve_careers("https://acme.invalid", fetch=fetch).platform == "workday"

    def test_robots_is_consulted_per_host(self):
        """A careers subdomain is a different host with its own robots.txt —
        the primary domain's permission does not carry over to it."""
        fetch = FakeFetcher(
            {
                "https://acme.invalid/": "<html><body>Brochure.</body></html>",
                "https://careers.acme.invalid/": CAREERS_LANDING_LINKING_TO_ATS,
            },
            robots={"https://careers.acme.invalid/robots.txt": "User-agent: *\nDisallow: /\n"},
        )
        resolve_careers("https://acme.invalid", fetch=fetch)
        assert "https://careers.acme.invalid/" not in fetch.page_calls

    def test_robots_allows_reads_the_hosts_own_rules(self):
        fetch = FakeFetcher({}, robots={"https://acme.invalid/robots.txt": "User-agent: *\nDisallow: /careers\n"})
        assert robots_allows("https://acme.invalid/careers", fetch=fetch) is False
        assert robots_allows("https://acme.invalid/about", fetch=fetch) is True


class TestRateLimiting:
    def test_the_crawl_uses_the_shared_rate_limited_fetcher(self):
        """resolve_careers defaults to job_discovery.base.polite_get, so the
        existing per-host interval applies to crawl traffic too — this is
        not a second, unthrottled HTTP path."""
        import src.job_discovery.base as base_module

        base_module._last_request_at.clear()
        with patch("src.job_discovery.base.requests.get") as mock_get, \
             patch("src.job_discovery.base.time.sleep") as mock_sleep:
            mock_get.return_value = MagicMock(status_code=404, text="")
            resolve_careers("https://ratelimit-crawl.invalid")
        assert mock_get.called
        mock_sleep.assert_called()


class TestNoFabrication:
    def test_the_resolver_never_produces_a_posting(self):
        """Separation of concerns as a safety property: this module has no
        way to emit a job, so it cannot invent one."""
        resolution = resolve_careers(
            "https://acme.invalid",
            fetch=FakeFetcher({"https://acme.invalid/careers": CAREERS_LANDING_LINKING_TO_ATS}),
        )
        assert isinstance(resolution, CareersResolution)
        assert not hasattr(resolution, "postings")

    def test_an_ats_vendor_named_in_prose_is_not_an_identifier(self):
        """Text mentioning a vendor is not a URL. Only a real URL counts."""
        fetch = FakeFetcher({
            "https://acme.invalid/careers": (
                "<html><body>Our applications are managed in Workday and PageUp.</body></html>"
            ),
        })
        resolution = resolve_careers("https://acme.invalid", fetch=fetch)
        assert resolution.platform == ""
        assert resolution.identifier == ""
        assert resolution.ats_label == ""
