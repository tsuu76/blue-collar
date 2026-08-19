"""
Tests for the job discovery source adapters (Greenhouse, Lever,
SmartRecruiters, Ashby) and shared base utilities (strip_html, rate
limiting). All HTTP is mocked with fixture JSON — no real network calls,
so these never hammer a real site during a test run.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.job_discovery.base import polite_get, strip_html
from src.job_discovery.sources.ashby import AshbySource
from src.job_discovery.sources.greenhouse import GreenhouseSource
from src.job_discovery.sources.lever import LeverSource
from src.job_discovery.sources.smartrecruiters import SmartRecruitersSource


class TestStripHtml:
    def test_strips_tags(self):
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_unescapes_entities(self):
        assert strip_html("Q&amp;A") == "Q&A"

    def test_converts_paragraphs_to_blank_lines(self):
        result = strip_html("<p>First</p><p>Second</p>")
        assert "First" in result and "Second" in result

    def test_empty_input_returns_empty(self):
        assert strip_html("") == ""

    def test_collapses_excess_whitespace(self):
        assert strip_html("<p>Too    many     spaces</p>") == "Too many spaces"

    def test_handles_html_entity_encoded_tags(self):
        # Regression: the real Greenhouse API returns its `content` field
        # with the HTML tags themselves entity-encoded (literally
        # "&lt;div&gt;", not "<div>") — found via live testing, not a
        # hypothetical. Unescaping must happen before tag-stripping, or the
        # tags never get removed.
        encoded = "&lt;div class=&quot;intro&quot;&gt;&lt;p&gt;Hello world&lt;/p&gt;&lt;/div&gt;"
        result = strip_html(encoded)
        assert result == "Hello world"
        assert "<" not in result and "&lt;" not in result


class TestPoliteGet:
    def test_sends_identifying_user_agent(self):
        with patch("src.job_discovery.base.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            polite_get("https://example.com/api")
        headers = mock_get.call_args.kwargs["headers"]
        assert "ITJobHunterBot" in headers["User-Agent"]

    def test_rate_limits_same_host(self):
        import src.job_discovery.base as base_module

        base_module._last_request_at.clear()
        with patch("src.job_discovery.base.requests.get") as mock_get, patch("src.job_discovery.base.time.sleep") as mock_sleep:
            mock_get.return_value = MagicMock(status_code=200)
            polite_get("https://ratelimit-test.example.com/a")
            polite_get("https://ratelimit-test.example.com/b")
        mock_sleep.assert_called()  # second request to the same host had to wait


GREENHOUSE_FIXTURE = {
    "jobs": [
        {
            "id": 12345,
            "title": "IT Support Officer",
            "location": {"name": "Sydney, NSW"},
            "content": "<p>Entry-level service desk role.</p><p>No experience required.</p>",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
            "company_name": "Acme",
        },
        {
            # Malformed: missing title — must be skipped, not crash the batch.
            "id": 99999,
            "location": {"name": "Sydney"},
        },
    ]
}


class TestGreenhouseSource:
    def test_discovers_and_normalizes_jobs(self):
        with patch("src.job_discovery.sources.greenhouse.polite_get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: GREENHOUSE_FIXTURE)
            mock_get.return_value.raise_for_status = lambda: None
            jobs = GreenhouseSource().discover("acme")

        assert len(jobs) == 1  # the malformed entry was skipped
        job = jobs[0]
        assert job.source == "greenhouse"
        assert job.title == "IT Support Officer"
        assert job.company == "Acme"
        assert job.location == "Sydney, NSW"
        assert job.source_job_id == "12345"
        assert "Entry-level service desk role." in job.description
        assert "<p>" not in job.description

    def test_http_error_propagates(self):
        with patch("src.job_discovery.sources.greenhouse.polite_get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404)
            mock_get.return_value.raise_for_status.side_effect = requests.HTTPError("404")
            with pytest.raises(requests.HTTPError):
                GreenhouseSource().discover("nonexistent-board")


LEVER_FIXTURE = [
    {
        "id": "abc-123",
        "text": "Service Desk Analyst",
        "categories": {"location": "Remote - Australia", "team": "IT"},
        "descriptionPlain": "Level 1 service desk role. 1 year experience.",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "company": "Acme",
    },
    {
        # Malformed: missing text (title) — must be skipped.
        "id": "def-456",
        "categories": {},
    },
]


class TestLeverSource:
    def test_discovers_and_normalizes_jobs(self):
        with patch("src.job_discovery.sources.lever.polite_get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: LEVER_FIXTURE)
            mock_get.return_value.raise_for_status = lambda: None
            jobs = LeverSource().discover("acme")

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "lever"
        assert job.title == "Service Desk Analyst"
        assert job.location == "Remote - Australia"
        assert job.source_job_id == "abc-123"
        assert job.company == "Acme"

    def test_falls_back_to_company_slug_when_no_company_field(self):
        fixture = [
            {"id": "1", "text": "IT Support", "categories": {}, "hostedUrl": "https://jobs.lever.co/beta/1"}
        ]
        with patch("src.job_discovery.sources.lever.polite_get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fixture)
            mock_get.return_value.raise_for_status = lambda: None
            jobs = LeverSource().discover("beta")
        assert jobs[0].company == "beta"


SMARTRECRUITERS_LIST_FIXTURE = {
    "content": [
        {
            "id": "posting-1",
            "name": "Desktop Support Technician",
            "location": {"city": "Sydney", "region": "NSW", "country": "Australia"},
            "company": {"name": "Acme"},
            "refNumber": "REQ-1",
        }
    ]
}
SMARTRECRUITERS_DETAIL_FIXTURE = {
    "jobAd": {"sections": {"jobDescription": {"text": "<p>Entry-level desktop support role.</p>"}}}
}


class TestSmartRecruitersSource:
    def test_discovers_and_normalizes_jobs_with_detail_fetch(self):
        with patch("src.job_discovery.sources.smartrecruiters.polite_get") as mock_get:
            list_resp = MagicMock(status_code=200, json=lambda: SMARTRECRUITERS_LIST_FIXTURE)
            list_resp.raise_for_status = lambda: None
            detail_resp = MagicMock(status_code=200, json=lambda: SMARTRECRUITERS_DETAIL_FIXTURE)
            detail_resp.raise_for_status = lambda: None
            mock_get.side_effect = [list_resp, detail_resp]

            jobs = SmartRecruitersSource().discover("acme")

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "smartrecruiters"
        assert job.title == "Desktop Support Technician"
        assert job.location == "Sydney, NSW, Australia"
        assert job.company == "Acme"
        assert job.source_job_id == "posting-1"
        assert "Entry-level desktop support role." in job.description

    def test_detail_fetch_failure_skips_just_that_posting(self):
        with patch("src.job_discovery.sources.smartrecruiters.polite_get") as mock_get:
            list_resp = MagicMock(status_code=200, json=lambda: SMARTRECRUITERS_LIST_FIXTURE)
            list_resp.raise_for_status = lambda: None
            mock_get.side_effect = [list_resp, requests.RequestException("timeout")]

            jobs = SmartRecruitersSource().discover("acme")

        assert jobs == []  # the one posting's detail fetch failed — skipped, not a crash


ASHBY_FIXTURE = {
    "jobs": [
        {
            "id": "ashby-1",
            "title": "Junior Application Support",
            "location": "Sydney, Australia",
            "descriptionPlain": "Graduate-friendly application support role.",
            "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-1",
        },
        {
            # Malformed: missing title.
            "id": "ashby-2",
        },
    ]
}


class TestAshbySource:
    def test_discovers_and_normalizes_jobs(self):
        with patch("src.job_discovery.sources.ashby.polite_get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: ASHBY_FIXTURE)
            mock_get.return_value.raise_for_status = lambda: None
            jobs = AshbySource().discover("acme")

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "ashby"
        assert job.title == "Junior Application Support"
        assert job.location == "Sydney, Australia"
        assert job.source_job_id == "ashby-1"
        assert job.company == "acme"
