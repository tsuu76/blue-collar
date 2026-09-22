"""
Tests for the outreach research layer.

No network: every test injects a fake adapter, so nothing here touches a real
ATS API. The postings below are obviously synthetic fixtures written for this
test file — they are not real companies or real job ads.
"""
from __future__ import annotations

import pytest

from src.job_discovery.base import JobDiscoverySource
from src.job_discovery.registry import EmployerConfig
from src.outreach.research import (
    RESOLVED_FROM_COMPANY,
    RESOLVED_FROM_EMPLOYERS,
    CompanyResearch,
    PostingResearch,
    extract_experience,
    extract_requirements,
    extract_skills,
    research_company,
    research_company_record,
    resolve_ats,
)
from src.sources.base import NormalizedJob

SAMPLE_DESCRIPTION = """About the role

We're hiring a Level 1 Service Desk Analyst to join our Sydney team.

What you'll need:
- 1+ years of experience in a helpdesk or service desk role
- Familiarity with Windows Server and Active Directory
- Basic scripting in PowerShell or Python
- Strong troubleshooting skills and a customer service mindset

Nice to have
- Exposure to Azure or Microsoft 365 administration
"""


def _posting(**overrides) -> NormalizedJob:
    return NormalizedJob(
        **{
            "source": "greenhouse",
            "url": "https://example.invalid/jobs/1",
            "title": "Level 1 Service Desk Analyst",
            "description": SAMPLE_DESCRIPTION,
            "company": "Example Co",
            "location": "Sydney, NSW",
            "source_job_id": "1",
            **overrides,
        }
    )


class FakeAdapter(JobDiscoverySource):
    """
    Stands in for a real ATS adapter. Either returns scripted postings or
    raises, so board-failure handling can be tested without a network.
    """

    platform = "greenhouse"

    def __init__(self, postings=None, error: Exception | None = None):
        self._postings = postings or []
        self._error = error
        self.calls: list[str] = []

    def discover(self, identifier: str) -> list[NormalizedJob]:
        self.calls.append(identifier)
        if self._error:
            raise self._error
        return list(self._postings)


@pytest.fixture()
def adapter():
    return FakeAdapter(postings=[_posting()])


@pytest.fixture()
def adapters(adapter):
    return {"greenhouse": adapter}


EMPLOYERS = [
    EmployerConfig(company="Example Co", platform="greenhouse", identifier="exampleco"),
    EmployerConfig(company="Other Co", platform="lever", identifier="otherco"),
]


class TestResolveAts:
    def test_company_record_wins(self):
        target = resolve_ats("Example Co", "greenhouse", "from-record", employers=EMPLOYERS)
        assert target.platform == "greenhouse"
        assert target.identifier == "from-record"
        assert target.resolved_from == RESOLVED_FROM_COMPANY

    def test_falls_back_to_employers_config(self):
        target = resolve_ats("Example Co", employers=EMPLOYERS)
        assert target.identifier == "exampleco"
        assert target.resolved_from == RESOLVED_FROM_EMPLOYERS

    def test_employer_match_is_case_and_space_insensitive(self):
        target = resolve_ats("  example co  ", employers=EMPLOYERS)
        assert target is not None
        assert target.identifier == "exampleco"

    def test_platform_is_normalized(self):
        target = resolve_ats("Example Co", "GREENHOUSE", "tok", employers=EMPLOYERS)
        assert target.platform == "greenhouse"

    def test_unknown_company_returns_none(self):
        assert resolve_ats("Nobody In The Registry", employers=EMPLOYERS) is None

    def test_blank_name_returns_none(self):
        assert resolve_ats("", employers=EMPLOYERS) is None

    def test_unsupported_platform_falls_back_to_config(self):
        """A company recorded with an unsupported ATS must not silently use
        it — but a valid employers.json match should still be honoured."""
        target = resolve_ats("Example Co", "pageup", "whatever", employers=EMPLOYERS)
        assert target is not None
        assert target.platform == "greenhouse"
        assert target.resolved_from == RESOLVED_FROM_EMPLOYERS

    def test_unsupported_platform_with_no_config_match_returns_none(self):
        assert resolve_ats("Unlisted Co", "pageup", "whatever", employers=EMPLOYERS) is None

    def test_platform_without_identifier_falls_back(self):
        target = resolve_ats("Example Co", "greenhouse", "", employers=EMPLOYERS)
        assert target.resolved_from == RESOLVED_FROM_EMPLOYERS


class TestResearchCompany:
    def test_reuses_existing_adapter(self, adapters, adapter):
        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        assert adapter.calls == ["exampleco"]
        assert research.has_postings is True
        assert research.platform == "greenhouse"

    def test_extracts_posting_fields(self, adapters):
        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        posting = research.postings[0]
        assert posting.title == "Level 1 Service Desk Analyst"
        assert posting.location == "Sydney, NSW"
        assert posting.url == "https://example.invalid/jobs/1"
        assert posting.source_job_id == "1"
        assert posting.description.startswith("About the role")
        assert posting.requirements
        assert posting.skills
        assert posting.experience

    def test_no_ats_returns_empty_research_not_an_error(self, adapters):
        research = research_company("Unknown Co", employers=[], adapters=adapters)
        assert research.has_postings is False
        assert research.postings == []
        assert "no supported ATS" in research.error

    def test_board_failure_is_recorded_not_raised(self):
        adapters = {"greenhouse": FakeAdapter(error=RuntimeError("404 Not Found"))}
        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        assert research.has_postings is False
        assert "404 Not Found" in research.error

    def test_empty_board_yields_no_postings(self):
        adapters = {"greenhouse": FakeAdapter(postings=[])}
        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        assert research.has_postings is False
        assert research.error == ""

    def test_missing_adapter_for_platform_is_recorded(self):
        research = research_company("Example Co", employers=EMPLOYERS, adapters={})
        assert "unsupported platform" in research.error

    def test_never_invents_postings(self, adapters):
        """The only postings that appear are the ones the adapter returned."""
        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        assert len(research.postings) == 1
        assert research.titles == ["Level 1 Service Desk Analyst"]

    def test_fetched_at_is_stamped(self, adapters):
        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        assert research.fetched_at

    def test_skills_union_across_postings(self):
        adapters = {
            "greenhouse": FakeAdapter(
                postings=[
                    _posting(description="We use Python and SQL here.", source_job_id="1"),
                    _posting(description="Docker and Python experience welcome.", source_job_id="2"),
                ]
            )
        }
        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        assert research.skills.count("Python") == 1
        assert set(research.skills) >= {"Python", "SQL", "Docker"}

    def test_to_dict_is_serializable(self, adapters):
        import json

        research = research_company("Example Co", employers=EMPLOYERS, adapters=adapters)
        payload = json.loads(json.dumps(research.to_dict()))
        assert payload["company"] == "Example Co"
        assert payload["postings"][0]["title"] == "Level 1 Service Desk Analyst"


class TestResearchCompanyRecord:
    def test_accepts_a_company_row(self, tmp_path, adapters, adapter):
        from src.database.db import get_connection, init_db
        from src.database.outreach_repo import get_company, insert_company

        db_path = tmp_path / "outreach.db"
        init_db(db_path)
        conn = get_connection(db_path)
        try:
            company_id = insert_company(
                conn,
                {
                    "name": "Example Co",
                    "website": "https://example.invalid",
                    "platform": "greenhouse",
                    "identifier": "from-record",
                },
            )
            conn.commit()
            row = get_company(conn, company_id)
        finally:
            conn.close()

        research = research_company_record(row, employers=EMPLOYERS, adapters=adapters)
        assert adapter.calls == ["from-record"]
        assert research.resolved_from == RESOLVED_FROM_COMPANY

    def test_row_without_ats_falls_back_to_employers_config(self, tmp_path, adapters, adapter):
        from src.database.db import get_connection, init_db
        from src.database.outreach_repo import get_company, insert_company

        db_path = tmp_path / "outreach.db"
        init_db(db_path)
        conn = get_connection(db_path)
        try:
            company_id = insert_company(conn, {"name": "Example Co", "website": "https://example.invalid"})
            conn.commit()
            row = get_company(conn, company_id)
        finally:
            conn.close()

        research = research_company_record(row, employers=EMPLOYERS, adapters=adapters)
        assert adapter.calls == ["exampleco"]
        assert research.resolved_from == RESOLVED_FROM_EMPLOYERS

    def test_plain_dict_works(self, adapters):
        research = research_company_record(
            {"name": "Example Co", "platform": "greenhouse", "identifier": "tok"},
            employers=EMPLOYERS,
            adapters=adapters,
        )
        assert research.identifier == "tok"

    def test_dict_without_optional_keys_does_not_raise(self, adapters):
        research = research_company_record({"name": "Example Co"}, employers=EMPLOYERS, adapters=adapters)
        assert research.resolved_from == RESOLVED_FROM_EMPLOYERS


class TestExtractRequirements:
    def test_pulls_bullets_under_a_requirements_heading(self):
        requirements = extract_requirements(SAMPLE_DESCRIPTION)
        assert any("helpdesk or service desk role" in r for r in requirements)
        assert any("Windows Server" in r for r in requirements)

    def test_strips_bullet_markers(self):
        for requirement in extract_requirements(SAMPLE_DESCRIPTION):
            assert not requirement.startswith(("-", "*", "•"))

    def test_returns_the_postings_own_words(self):
        """Requirements are sliced verbatim, never paraphrased."""
        for requirement in extract_requirements(SAMPLE_DESCRIPTION):
            assert requirement in SAMPLE_DESCRIPTION

    def test_empty_description_returns_empty(self):
        assert extract_requirements("") == []

    def test_prose_without_bullets_returns_nothing(self):
        assert extract_requirements("We are a company. We do things. Apply now.") == []

    def test_ignores_very_short_fragments(self):
        assert extract_requirements("Requirements:\n- ok\n- SQL and Python experience needed") == [
            "SQL and Python experience needed"
        ]

    def test_caps_the_number_returned(self):
        description = "Requirements:\n" + "\n".join(f"- Requirement number {i} goes here" for i in range(40))
        assert len(extract_requirements(description)) == 12

    def test_deduplicates(self):
        description = "Requirements:\n- Experience with SQL databases\n- Experience with SQL databases"
        assert len(extract_requirements(description)) == 1


class TestExtractExperience:
    def test_captures_the_surrounding_phrase(self):
        phrases = extract_experience(SAMPLE_DESCRIPTION)
        assert any("1+ years" in p for p in phrases)
        assert any("helpdesk" in p for p in phrases)

    def test_handles_ranges(self):
        assert extract_experience("We want 2-4 years of relevant experience.")

    def test_no_experience_mentioned_returns_empty(self):
        assert extract_experience("A great place to work.") == []

    def test_empty_description_returns_empty(self):
        assert extract_experience("") == []

    def test_caps_the_number_returned(self):
        description = " ".join(f"Needs {i} years of experience." for i in range(20))
        assert len(extract_experience(description)) <= 5


class TestExtractSkills:
    def test_finds_named_technologies(self):
        skills = extract_skills(SAMPLE_DESCRIPTION)
        assert {"Python", "PowerShell", "Windows Server", "Azure"} <= set(skills)

    def test_is_case_insensitive(self):
        assert "Python" in extract_skills("we use PYTHON daily")

    def test_does_not_invent_absent_technologies(self):
        """The critical property: a technology appears only if the posting
        actually names it."""
        skills = extract_skills("We are looking for someone friendly and organised.")
        assert skills == []

    def test_java_does_not_match_javascript(self):
        skills = extract_skills("Strong JavaScript skills required.")
        assert "JavaScript" in skills
        assert "Java" not in skills

    def test_canonical_spelling_is_returned(self):
        assert "AWS" in extract_skills("experience with aws is a plus")

    def test_empty_text_returns_empty(self):
        assert extract_skills("") == []


class TestDataclasses:
    def test_posting_from_normalized_truncates_long_descriptions(self):
        posting = PostingResearch.from_normalized(_posting(description="x" * 20000))
        assert len(posting.description) == 8000

    def test_empty_research_reports_no_postings(self):
        assert CompanyResearch(company="Example Co").has_postings is False

    def test_titles_deduplicates(self):
        research = CompanyResearch(
            company="Example Co",
            postings=[
                PostingResearch(title="IT Support", url="", location="", description=""),
                PostingResearch(title="IT Support", url="", location="", description=""),
            ],
        )
        assert research.titles == ["IT Support"]
