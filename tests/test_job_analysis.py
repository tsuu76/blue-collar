"""
Tests for Phase 8 (AI job analysis): shape validation, retry-then-fail-safe
behavior, and the schemas module directly. No real Ollama calls — uses
FakeAIProvider to script responses.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.ai.base import AIResponseError
from src.ai.job_analysis import analyze_job, build_prompt
from src.ai.schemas import JobAnalysis, QualityControlResult
from tests.fakes import FakeAIProvider

VALID_ANALYSIS = {
    "category": "ENTRY_LEVEL_IT",
    "fit_score": 88,
    "experience_required": "0-1 years",
    "desk_based": True,
    "recommendation": "APPLY",
    "matched_skills": ["Python"],
    "missing_skills": ["Active Directory"],
    "relevant_keywords": ["service desk"],
    "reasons": ["Entry-level service desk role"],
    "concerns": [],
}


class TestJobAnalysisSchema:
    def test_valid_shape_validates(self):
        analysis = JobAnalysis.model_validate(VALID_ANALYSIS)
        assert analysis.fit_score == 88
        assert analysis.category == "ENTRY_LEVEL_IT"

    def test_fit_score_out_of_range_rejected(self):
        bad = dict(VALID_ANALYSIS, fit_score=150)
        with pytest.raises(ValidationError):
            JobAnalysis.model_validate(bad)

    def test_negative_fit_score_rejected(self):
        bad = dict(VALID_ANALYSIS, fit_score=-5)
        with pytest.raises(ValidationError):
            JobAnalysis.model_validate(bad)

    def test_invalid_category_rejected(self):
        bad = dict(VALID_ANALYSIS, category="SUPER_SENIOR")
        with pytest.raises(ValidationError):
            JobAnalysis.model_validate(bad)

    def test_invalid_recommendation_rejected(self):
        bad = dict(VALID_ANALYSIS, recommendation="MAYBE")
        with pytest.raises(ValidationError):
            JobAnalysis.model_validate(bad)

    def test_missing_required_key_rejected(self):
        bad = dict(VALID_ANALYSIS)
        del bad["fit_score"]
        with pytest.raises(ValidationError):
            JobAnalysis.model_validate(bad)

    def test_category_case_insensitive(self):
        lower = dict(VALID_ANALYSIS, category="entry_level_it")
        analysis = JobAnalysis.model_validate(lower)
        assert analysis.category == "ENTRY_LEVEL_IT"

    def test_desk_based_must_be_bool(self):
        # Note: pydantic's lax bool coercion accepts strings like "yes"/"true"
        # as valid booleans (useful since an LLM might emit either form), so
        # this test uses a value with no sensible bool coercion at all.
        bad = dict(VALID_ANALYSIS, desk_based=["not", "a", "bool"])
        with pytest.raises(ValidationError):
            JobAnalysis.model_validate(bad)


class TestQualityControlSchema:
    def test_passed_result(self):
        result = QualityControlResult.model_validate({"passed": True, "issues": []})
        assert result.passed is True

    def test_failed_result_with_issues(self):
        result = QualityControlResult.model_validate({"passed": False, "issues": ["fabricated skill: Kubernetes"]})
        assert result.passed is False
        assert result.issues == ["fabricated skill: Kubernetes"]

    def test_missing_issues_defaults_empty(self):
        result = QualityControlResult.model_validate({"passed": True})
        assert result.issues == []


class TestBuildPrompt:
    def test_includes_title_description_and_skills(self):
        prompt = build_prompt("IT Support Officer", "Some description", ["Python", "SQL"])
        assert "IT Support Officer" in prompt
        assert "Some description" in prompt
        assert "Python" in prompt
        assert "SQL" in prompt

    def test_empty_skills_list_handled(self):
        prompt = build_prompt("IT Support Officer", "Some description", [])
        assert "(none listed)" in prompt


class TestAnalyzeJobRetryBehavior:
    def test_succeeds_on_first_valid_response(self):
        provider = FakeAIProvider([VALID_ANALYSIS])
        result = analyze_job(
            title="IT Support Officer",
            description="desc",
            candidate_skills=["Python"],
            provider=provider,
        )
        assert result.fit_score == 88
        assert provider.call_count == 1

    def test_retries_after_invalid_shape_then_succeeds(self):
        bad_shape = dict(VALID_ANALYSIS, fit_score=999)  # invalid: out of range
        provider = FakeAIProvider([bad_shape, VALID_ANALYSIS])
        result = analyze_job(
            title="IT Support Officer",
            description="desc",
            candidate_skills=["Python"],
            provider=provider,
            max_retries=2,
        )
        assert result.fit_score == 88
        assert provider.call_count == 2

    def test_retries_after_provider_error_then_succeeds(self):
        provider = FakeAIProvider([AIResponseError("timeout"), VALID_ANALYSIS])
        result = analyze_job(
            title="IT Support Officer",
            description="desc",
            candidate_skills=["Python"],
            provider=provider,
            max_retries=2,
        )
        assert result.fit_score == 88

    def test_fails_safe_after_exhausting_retries(self):
        bad_shape = dict(VALID_ANALYSIS, fit_score=999)
        provider = FakeAIProvider([bad_shape, bad_shape, bad_shape])
        with pytest.raises(AIResponseError):
            analyze_job(
                title="IT Support Officer",
                description="desc",
                candidate_skills=["Python"],
                provider=provider,
                max_retries=2,
            )
        assert provider.call_count == 3

    def test_never_raises_a_bare_exception_type(self):
        # Callers only need to catch AIResponseError, never ValidationError
        # or another internal exception type leaking out.
        bad_shape = {"nonsense": True}
        provider = FakeAIProvider([bad_shape])
        with pytest.raises(AIResponseError):
            analyze_job(
                title="IT Support Officer",
                description="desc",
                candidate_skills=["Python"],
                provider=provider,
                max_retries=0,
            )
