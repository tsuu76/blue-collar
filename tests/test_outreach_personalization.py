"""
Tests for the outreach personalization analysis.

No network and no Ollama: every test uses FakeAIProvider from tests/fakes.py.
The company, postings and resume below are synthetic fixtures written for
this file.
"""
from __future__ import annotations

import pytest

from src.ai.base import AIResponseError
from src.ai.schemas import OutreachAnalysis
from src.outreach.personalization import (
    analyze_company,
    build_prompt,
    verify_analysis,
)
from src.outreach.research import CompanyResearch, PostingResearch
from src.resume.schema import Bullet, Education, MasterResume, Personal, Project
from tests.fakes import FakeAIProvider


@pytest.fixture()
def resume() -> MasterResume:
    return MasterResume(
        personal=Personal(full_name="Test Candidate", email="test@example.invalid"),
        summary="First-year IT student.",
        skills=["Python", "SQL", "Git", "Kali Linux"],
        education=[
            Education(
                id="edu_01",
                institution="Example University",
                credential="Bachelor of IT",
                highlights=[Bullet(id="edu_01_b1", text="Studied networking fundamentals.")],
            )
        ],
        projects=[
            Project(
                id="proj_01",
                name="TestApp",
                technologies=["Python", "SQL"],
                bullets=[Bullet(id="proj_01_b1", text="Built a small tracker in Python.", skills=["Python"])],
            )
        ],
    )


@pytest.fixture()
def research() -> CompanyResearch:
    return CompanyResearch(
        company="Example Co",
        platform="greenhouse",
        identifier="exampleco",
        postings=[
            PostingResearch(
                title="Service Desk Analyst",
                url="https://example.invalid/1",
                location="Sydney",
                description="You will troubleshoot issues and write Python scripts. SQL knowledge helps.",
            ),
            PostingResearch(
                title="Junior Support Engineer",
                url="https://example.invalid/2",
                location="Sydney",
                description="Support our platform. Python and SQL are used daily by the team.",
            ),
        ],
    )


FULL_ANALYSIS = {
    "recurring_skills": ["Python", "SQL"],
    "tools_and_technologies": ["Python"],
    "responsibilities": ["Troubleshoot issues raised by customers"],
    "experience_requirements": ["Entry level"],
    "terminology": ["Service Desk"],
    "candidate_overlap": ["Python", "SQL"],
    "notes": "They hire support-focused technical staff.",
}


class TestVerifyAnalysis:
    def test_keeps_terms_present_in_postings(self, research, resume):
        result = verify_analysis(OutreachAnalysis(**FULL_ANALYSIS), research, resume)
        assert result.analysis.recurring_skills == ["Python", "SQL"]
        assert result.dropped_company_terms == []

    def test_drops_technology_the_postings_never_mention(self, research, resume):
        """The model naming Kubernetes for a company that never mentioned it
        must not reach the email."""
        analysis = OutreachAnalysis(**{**FULL_ANALYSIS, "recurring_skills": ["Python", "Kubernetes"]})
        result = verify_analysis(analysis, research, resume)
        assert result.analysis.recurring_skills == ["Python"]
        assert "Kubernetes" in result.dropped_company_terms

    def test_drops_invented_tools_and_terminology(self, research, resume):
        analysis = OutreachAnalysis(
            **{**FULL_ANALYSIS, "tools_and_technologies": ["Terraform"], "terminology": ["synergy"]}
        )
        result = verify_analysis(analysis, research, resume)
        assert result.analysis.tools_and_technologies == []
        assert result.analysis.terminology == []
        assert set(result.dropped_company_terms) == {"Terraform", "synergy"}

    def test_drops_skill_the_candidate_does_not_have(self, research, resume):
        """The central guarantee: a claimed overlap that isn't in the resume
        is removed, whatever the model said."""
        analysis = OutreachAnalysis(**{**FULL_ANALYSIS, "candidate_overlap": ["Python", "Java", "AWS"]})
        result = verify_analysis(analysis, research, resume)
        assert result.analysis.candidate_overlap == ["Python"]
        assert set(result.dropped_candidate_claims) == {"Java", "AWS"}

    def test_overlap_uses_the_resumes_own_spelling(self, research, resume):
        analysis = OutreachAnalysis(**{**FULL_ANALYSIS, "candidate_overlap": ["python", "sql"]})
        result = verify_analysis(analysis, research, resume)
        assert result.analysis.candidate_overlap == ["Python", "SQL"]

    def test_overlap_phrase_maps_back_to_the_real_skill(self, research, resume):
        analysis = OutreachAnalysis(**{**FULL_ANALYSIS, "candidate_overlap": ["Python scripting"]})
        result = verify_analysis(analysis, research, resume)
        assert result.analysis.candidate_overlap == ["Python"]

    def test_overlap_can_be_emptied_entirely(self, research, resume):
        analysis = OutreachAnalysis(**{**FULL_ANALYSIS, "candidate_overlap": ["Rust", "Go"]})
        result = verify_analysis(analysis, research, resume)
        assert result.analysis.candidate_overlap == []
        assert result.has_overlap is False

    def test_deduplicates_overlap(self, research, resume):
        analysis = OutreachAnalysis(**{**FULL_ANALYSIS, "candidate_overlap": ["Python", "python", "Python scripting"]})
        result = verify_analysis(analysis, research, resume)
        assert result.analysis.candidate_overlap == ["Python"]

    def test_to_dict_is_serializable(self, research, resume):
        import json

        result = verify_analysis(OutreachAnalysis(**FULL_ANALYSIS), research, resume)
        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["analysis"]["candidate_overlap"] == ["Python", "SQL"]


class TestBuildPrompt:
    def test_includes_real_postings_and_candidate_skills(self, research, resume):
        prompt = build_prompt(research, resume)
        assert "Service Desk Analyst" in prompt
        assert "Junior Support Engineer" in prompt
        assert "Python" in prompt
        assert "Example Co" in prompt

    def test_states_the_candidate_has_nothing_beyond_the_list(self, research, resume):
        assert "nothing more" in build_prompt(research, resume)


class TestAnalyzeCompany:
    def test_returns_verified_result(self, research, resume):
        provider = FakeAIProvider(json_responses=[FULL_ANALYSIS])
        result = analyze_company(research, resume, provider=provider)
        assert result.analysis.candidate_overlap == ["Python", "SQL"]
        assert provider.call_count == 1

    def test_filters_the_models_output(self, research, resume):
        provider = FakeAIProvider(
            json_responses=[{**FULL_ANALYSIS, "candidate_overlap": ["Python", "Kubernetes"]}]
        )
        result = analyze_company(research, resume, provider=provider)
        assert result.analysis.candidate_overlap == ["Python"]

    def test_retries_on_malformed_output(self, research, resume):
        # Wrong type, not merely an unexpected key: every field has a default
        # and pydantic ignores extras, so an unknown key is not a shape error.
        provider = FakeAIProvider(json_responses=[{"recurring_skills": "not a list"}, FULL_ANALYSIS])
        result = analyze_company(research, resume, provider=provider, max_retries=2)
        assert result.analysis.recurring_skills == ["Python", "SQL"]
        assert provider.call_count == 2

    def test_raises_when_retries_exhausted(self, research, resume):
        provider = FakeAIProvider(json_responses=[{"recurring_skills": "not a list"}] * 3)
        with pytest.raises(AIResponseError):
            analyze_company(research, resume, provider=provider, max_retries=2)

    def test_refuses_when_there_are_no_postings(self, resume):
        """No real postings means nothing honest to personalize from."""
        empty = CompanyResearch(company="Example Co", error="no supported ATS board configured")
        with pytest.raises(ValueError, match="nothing to personalize"):
            analyze_company(empty, resume, provider=FakeAIProvider(json_responses=[FULL_ANALYSIS]))

    def test_no_postings_makes_no_ai_call(self, resume):
        provider = FakeAIProvider(json_responses=[FULL_ANALYSIS])
        with pytest.raises(ValueError):
            analyze_company(CompanyResearch(company="Example Co"), resume, provider=provider)
        assert provider.call_count == 0
