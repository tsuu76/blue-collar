"""
Verify that outreach research records the AU location-relevance of the
postings it found. This is what stops the outreach path from
recommending companies whose 609 open postings are all overseas — the
research summary now has to say "0 in target region", and the
dashboard/pipeline can act on that.
"""
from __future__ import annotations

import dataclasses

import pytest

from src.outreach.research import PostingResearch, research_company
from src.sources.base import NormalizedJob


class _FakeAdapter:
    """
    Minimal drop-in for a src.job_discovery adapter — returns a
    hard-coded list of NormalizedJob objects so research_company runs
    with zero HTTP, zero network, and no coupling to a real ATS.
    """

    platform = "greenhouse"

    def __init__(self, jobs: list[NormalizedJob]):
        self._jobs = jobs

    def discover(self, identifier: str) -> list[NormalizedJob]:
        return list(self._jobs)


def _job(title: str, location: str, url_slug: str) -> NormalizedJob:
    return NormalizedJob(
        source="greenhouse",
        url=f"https://example.com/jobs/{url_slug}",
        title=title,
        description="Body text.",
        company="Example",
        location=location,
        source_job_id=url_slug,
    )


@pytest.fixture(autouse=True)
def _sydney_target_locations(monkeypatch):
    """
    Pin `settings.target_locations` for this file so the tests don't
    depend on whatever's in .env at run time.
    """
    from src import config
    from src.outreach import relevance

    pinned = dataclasses.replace(
        config.settings,
        target_locations=["Sydney", "NSW", "Remote Australia"],
    )
    monkeypatch.setattr(relevance, "settings", pinned)


class TestResearchRecordsLocationRelevance:
    def test_zero_matches_when_all_overseas(self):
        adapter = _FakeAdapter([
            _job("Backend Engineer", "Amsterdam", "1"),
            _job("Sales Manager", "Singapore", "2"),
            _job("Support Officer", "London", "3"),
        ])
        research = research_company(
            "Example",
            platform="greenhouse",
            identifier="example",
            adapters={"greenhouse": adapter},
            employers=[],
        )
        assert len(research.postings) == 3
        assert research.location_relevance.matching == 0
        assert research.location_relevance.total == 3
        assert research.location_relevance.any_match is False

    def test_partial_matches_when_some_in_target(self):
        adapter = _FakeAdapter([
            _job("Backend Engineer", "Sydney NSW", "1"),
            _job("Sales Manager", "Singapore", "2"),
            _job("Support Officer", "Remote Australia", "3"),
            _job("Marketing", "London", "4"),
        ])
        research = research_company(
            "Example",
            platform="greenhouse",
            identifier="example",
            adapters={"greenhouse": adapter},
            employers=[],
        )
        assert research.location_relevance.matching == 2
        assert research.location_relevance.total == 4
        assert research.location_relevance.any_match is True

    def test_summary_and_to_dict_carry_relevance(self):
        adapter = _FakeAdapter([_job("Analyst", "Sydney", "1")])
        research = research_company(
            "Example",
            platform="greenhouse",
            identifier="example",
            adapters={"greenhouse": adapter},
            employers=[],
        )
        summary = research.summary()
        assert summary["location_relevance"] == {"matching": 1, "total": 1}
        assert research.to_dict()["location_relevance"] == {"matching": 1, "total": 1}

    def test_error_path_leaves_relevance_at_zero_zero(self):
        # No adapter registered — research bails with an error and
        # the relevance stays at its default (0, 0) rather than being
        # silently omitted.
        research = research_company(
            "Example",
            platform="greenhouse",
            identifier="example",
            adapters={},
            employers=[],
        )
        assert research.error
        assert research.location_relevance.total == 0
        assert research.location_relevance.matching == 0
