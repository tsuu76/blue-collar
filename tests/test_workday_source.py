"""
Tests for the Workday career-site adapter.

No network: every test patches `polite_post` with a fake that returns the
shape Workday's `/wday/cxs/{tenant}/{site}/jobs` endpoint really returns.

The two properties that matter most here are the ones a mistake would make
dangerous rather than merely wrong: the adapter must refuse to act on
anything that isn't a genuinely discovered Workday URL, and it must never
turn an incomplete API entry into a plausible-looking posting.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.job_discovery.registry import ADAPTERS
from src.job_discovery.sources.workday import (
    MAX_PAGES,
    PAGE_LIMIT,
    WorkdaySource,
    parse_identifier,
)

SITE_URL = "https://acme.wd3.myworkdayjobs.com/acme/Acme_Careers"


def _posting(title="Cyber Security Analyst", path="/job/Sydney/Cyber-Security-Analyst_R-1", location="Sydney"):
    return {"title": title, "externalPath": path, "locationsText": location, "bulletFields": ["R-1"]}


class FakePost:
    """Stands in for polite_post; records each request body so paging can be
    asserted on."""

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.bodies: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, url, json=None, timeout=None, headers=None, **kwargs):
        self.urls.append(url)
        self.bodies.append(json)
        index = len(self.bodies) - 1
        payload = self.pages[index] if index < len(self.pages) else {"total": 0, "jobPostings": []}
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response


def _run(source, identifier, fake):
    with patch("src.job_discovery.sources.workday.polite_post", fake):
        return source.discover(identifier)


class TestParseIdentifier:
    @pytest.mark.parametrize(
        "url,expected",
        [
            (SITE_URL, ("acme.wd3.myworkdayjobs.com", "acme", "Acme_Careers")),
            ("https://acme.wd3.myworkdayjobs.com/Acme_Careers", ("acme.wd3.myworkdayjobs.com", "acme", "Acme_Careers")),
            ("https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers", ("acme.wd3.myworkdayjobs.com", "acme", "Acme_Careers")),
            ("https://bhp.wd3.myworkdayjobs.com/en-US/BHP", ("bhp.wd3.myworkdayjobs.com", "bhp", "BHP")),
            (
                "https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers/job/Sydney/Analyst_R-1",
                ("acme.wd3.myworkdayjobs.com", "acme", "Acme_Careers"),
            ),
        ],
    )
    def test_real_workday_url_shapes(self, url, expected):
        assert parse_identifier(url) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "acme",                                        # a bare company slug
            "https://acme.com/careers",                    # not Workday at all
            "https://acme.wd3.myworkdayjobs.com/",         # no career site named
            "https://acme.wd3.myworkdayjobs.com/en-US",    # locale only
            "ftp://acme.wd3.myworkdayjobs.com/Acme",       # not http(s)
            "https://myworkdayjobs.com/Acme",              # no tenant label
        ],
    )
    def test_anything_that_is_not_a_discovered_workday_url_is_refused(self, value):
        assert parse_identifier(value) is None


class TestDiscover:
    def test_reads_real_postings(self):
        fake = FakePost([{"total": 2, "jobPostings": [
            _posting(),
            _posting("Service Desk Analyst", "/job/Melbourne/Service-Desk_R-2", "Melbourne"),
        ]}])
        jobs = _run(WorkdaySource(), SITE_URL, fake)

        assert [job.title for job in jobs] == ["Cyber Security Analyst", "Service Desk Analyst"]
        assert jobs[0].url == "https://acme.wd3.myworkdayjobs.com/Acme_Careers/job/Sydney/Cyber-Security-Analyst_R-1"
        assert jobs[0].location == "Sydney"
        assert jobs[0].source == "workday"
        assert jobs[0].source_job_id == "/job/Sydney/Cyber-Security-Analyst_R-1"

    def test_queries_the_endpoint_derived_from_the_discovered_url(self):
        fake = FakePost([{"total": 1, "jobPostings": [_posting()]}])
        _run(WorkdaySource(), SITE_URL, fake)
        assert fake.urls[0] == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Acme_Careers/jobs"

    def test_a_non_workday_identifier_is_never_queried(self):
        """Requirement 7: no tenant, no request. A company whose site never
        exposed a Workday URL must produce no Workday traffic at all."""
        fake = FakePost([{"total": 99, "jobPostings": [_posting()]}])
        assert _run(WorkdaySource(), "acme", fake) == []
        assert fake.urls == []

    def test_pages_until_the_total_is_reached(self):
        fake = FakePost([
            {"total": 25, "jobPostings": [_posting(f"Role {n}", f"/job/Sydney/R-{n}") for n in range(20)]},
            {"total": 25, "jobPostings": [_posting(f"Role {n}", f"/job/Sydney/R-{n}") for n in range(20, 25)]},
        ])
        jobs = _run(WorkdaySource(), SITE_URL, fake)
        assert len(jobs) == 25
        assert [body["offset"] for body in fake.bodies] == [0, PAGE_LIMIT]

    def test_paging_is_capped(self):
        """A very large employer costs a bounded number of requests, not one
        per twenty roles."""
        pages = [
            {
                "total": 5000,
                "jobPostings": [
                    _posting(f"Role {p * 20 + n}", f"/job/Sydney/R-{p * 20 + n}") for n in range(20)
                ],
            }
            for p in range(10)
        ]
        fake = FakePost(pages)
        jobs = _run(WorkdaySource(), SITE_URL, fake)
        assert len(fake.bodies) == MAX_PAGES
        assert len(jobs) == MAX_PAGES * PAGE_LIMIT

    def test_an_empty_board_is_an_empty_list_not_an_error(self):
        fake = FakePost([{"total": 0, "jobPostings": []}])
        assert _run(WorkdaySource(), SITE_URL, fake) == []

    def test_duplicate_postings_across_pages_are_collapsed(self):
        """Workday repeats entries when a board changes mid-paging. The same
        externalPath must never become two jobs."""
        repeated = _posting("Cyber Security Analyst", "/job/Sydney/Cyber_R-1")
        fake = FakePost([
            {"total": 40, "jobPostings": [repeated, _posting("Other", "/job/Perth/Other_R-2")]},
            {"total": 40, "jobPostings": [repeated]},
        ])
        jobs = _run(WorkdaySource(), SITE_URL, fake)
        assert len(jobs) == 2
        assert len({job.source_job_id for job in jobs}) == 2

    def test_a_fetch_failure_propagates_like_the_other_adapters(self):
        def boom(url, **kwargs):
            response = MagicMock()
            response.raise_for_status.side_effect = RuntimeError("503 Service Unavailable")
            return response

        with patch("src.job_discovery.sources.workday.polite_post", boom):
            with pytest.raises(RuntimeError):
                WorkdaySource().discover(SITE_URL)


class TestNoFabrication:
    def test_a_posting_with_no_title_is_dropped_not_filled_in(self):
        fake = FakePost([{"total": 2, "jobPostings": [
            {"externalPath": "/job/Sydney/Mystery_R-9", "locationsText": "Sydney"},
            _posting(),
        ]}])
        jobs = _run(WorkdaySource(), SITE_URL, fake)
        assert [job.title for job in jobs] == ["Cyber Security Analyst"]

    def test_a_posting_with_no_path_never_gets_an_invented_url(self):
        fake = FakePost([{"total": 1, "jobPostings": [{"title": "Ghost Role", "locationsText": "Sydney"}]}])
        assert _run(WorkdaySource(), SITE_URL, fake) == []

    def test_a_missing_description_stays_empty(self):
        """The list endpoint carries no body text. An empty description is
        the truthful value — it must not be synthesised from the title,
        the location, or the bulletFields."""
        fake = FakePost([{"total": 1, "jobPostings": [_posting()]}])
        job = _run(WorkdaySource(), SITE_URL, fake)[0]
        assert job.description == ""

    def test_a_garbage_response_yields_nothing(self):
        fake = FakePost([{"total": 3, "jobPostings": ["not a dict", None, 42]}])
        assert _run(WorkdaySource(), SITE_URL, fake) == []


class TestRegistryWiring:
    def test_workday_is_registered(self):
        assert isinstance(ADAPTERS["workday"], WorkdaySource)
        assert ADAPTERS["workday"].platform == "workday"

    def test_the_existing_adapters_are_untouched(self):
        """Stage 2 adds a platform; it must not have altered the four that
        already worked, nor the careers-page fallback."""
        from src.job_discovery.sources.ashby import AshbySource
        from src.job_discovery.sources.generic_careers import GenericCareersSource
        from src.job_discovery.sources.greenhouse import GreenhouseSource
        from src.job_discovery.sources.lever import LeverSource
        from src.job_discovery.sources.smartrecruiters import SmartRecruitersSource

        assert isinstance(ADAPTERS["greenhouse"], GreenhouseSource)
        assert isinstance(ADAPTERS["lever"], LeverSource)
        assert isinstance(ADAPTERS["ashby"], AshbySource)
        assert isinstance(ADAPTERS["smartrecruiters"], SmartRecruitersSource)
        assert isinstance(ADAPTERS["careers_page"], GenericCareersSource)


class TestRateLimiting:
    def test_workday_requests_go_through_the_shared_rate_limiter(self):
        import src.job_discovery.base as base_module

        base_module._last_request_at.clear()
        page = {"total": 100, "jobPostings": [_posting(f"Role {n}", f"/job/Sydney/R-{n}") for n in range(20)]}
        with patch("src.job_discovery.base.requests.post") as mock_post, \
             patch("src.job_discovery.base.time.sleep") as mock_sleep:
            response = MagicMock(status_code=200)
            response.json.return_value = page
            mock_post.return_value = response
            WorkdaySource().discover(SITE_URL)
        assert mock_post.called
        mock_sleep.assert_called()  # the second page had to wait for the same host

    def test_it_sends_the_identifying_user_agent(self):
        import src.job_discovery.base as base_module

        base_module._last_request_at.clear()
        with patch("src.job_discovery.base.requests.post") as mock_post, \
             patch("src.job_discovery.base.time.sleep"):
            response = MagicMock(status_code=200)
            response.json.return_value = {"total": 0, "jobPostings": []}
            mock_post.return_value = response
            WorkdaySource().discover(SITE_URL)
        assert "ITJobHunterBot" in mock_post.call_args.kwargs["headers"]["User-Agent"]
