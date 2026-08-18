"""
Tests for Phase 9 job scoring — most importantly the hard veto rule from
spec section 16: "Senior job + 95% skill match = REJECT." A high weighted
score must never rescue a job the AI itself categorized as senior/non-IT.
"""
from __future__ import annotations

from src.ai.schemas import JobAnalysis
from src.job_filter.cheap_filter import CheapFilterResult
from src.job_filter.scoring import score_job

PASSING_CHEAP_RESULT = CheapFilterResult(
    passed=True,
    reasons=["Matched IT role keyword(s): ['it support']"],
    matched_positive_keywords=["it support", "entry level"],
    min_years_required=0,
    location_ok=True,
)

ENTRY_LEVEL_ANALYSIS = JobAnalysis(
    category="ENTRY_LEVEL_IT",
    fit_score=90,
    experience_required="0-1 years",
    desk_based=True,
    recommendation="APPLY",
    matched_skills=["Python", "Git"],
    missing_skills=["Active Directory"],
    relevant_keywords=["service desk"],
    reasons=["Entry-level role"],
    concerns=[],
)


class TestHardVeto:
    def test_senior_category_with_perfect_skill_match_is_rejected(self):
        # This is the exact scenario from the spec: senior job, 95%+ skill
        # match — must still be a hard reject, total_score forced to 0.
        senior_analysis = JobAnalysis(
            category="MID_SENIOR_IT",
            fit_score=95,
            experience_required="5+ years",
            desk_based=True,
            recommendation="APPLY",  # even if the AI itself misjudges the recommendation
            matched_skills=["Python", "Git", "GitHub", "SQL", "Networking"],
            missing_skills=["Kubernetes"],  # 5/6 = ~95% skill match
            relevant_keywords=[],
            reasons=[],
            concerns=[],
        )
        result = score_job(PASSING_CHEAP_RESULT, senior_analysis)
        assert result.vetoed is True
        assert result.total_score == 0
        assert result.qualified is False
        assert "MID_SENIOR_IT" in result.veto_reason

    def test_non_it_category_is_rejected_regardless_of_skills(self):
        non_it_analysis = JobAnalysis(
            category="NON_IT",
            fit_score=80,
            experience_required="0-1 years",
            desk_based=True,
            recommendation="APPLY",
            matched_skills=["Python", "Excel"],
            missing_skills=[],
            relevant_keywords=[],
            reasons=[],
            concerns=[],
        )
        result = score_job(PASSING_CHEAP_RESULT, non_it_analysis)
        assert result.vetoed is True
        assert result.total_score == 0
        assert result.qualified is False

    def test_cheap_filter_rejection_short_circuits_before_scoring(self):
        rejected_cheap_result = CheapFilterResult(
            passed=False,
            reasons=["Rejected: seniority keyword(s) found"],
            rejection_category="SENIORITY",
        )
        result = score_job(rejected_cheap_result, ENTRY_LEVEL_ANALYSIS)
        assert result.vetoed is True
        assert result.total_score == 0


class TestNormalScoring:
    def test_strong_entry_level_match_scores_highly(self):
        result = score_job(PASSING_CHEAP_RESULT, ENTRY_LEVEL_ANALYSIS)
        assert result.vetoed is False
        assert result.total_score >= 75
        assert result.qualified is True

    def test_qualified_flag_respects_min_fit_score_threshold(self):
        result = score_job(PASSING_CHEAP_RESULT, ENTRY_LEVEL_ANALYSIS, min_fit_score=99)
        assert result.qualified is False  # score is high but below an artificially high threshold

    def test_unclear_category_scores_lower_than_entry_level(self):
        unclear_analysis = JobAnalysis(
            category="UNCLEAR",
            fit_score=50,
            experience_required="unclear",
            desk_based=False,
            recommendation="REVIEW",
            matched_skills=["Python"],
            missing_skills=["Excel"],
            relevant_keywords=[],
            reasons=[],
            concerns=["Unclear job description"],
        )
        entry_result = score_job(PASSING_CHEAP_RESULT, ENTRY_LEVEL_ANALYSIS)
        unclear_result = score_job(PASSING_CHEAP_RESULT, unclear_analysis)
        assert unclear_result.total_score < entry_result.total_score

    def test_no_skills_data_gives_neutral_skills_component(self):
        no_skills_analysis = JobAnalysis(
            category="ENTRY_LEVEL_IT",
            fit_score=70,
            experience_required="0-1 years",
            desk_based=True,
            recommendation="APPLY",
            matched_skills=[],
            missing_skills=[],
            relevant_keywords=[],
            reasons=[],
            concerns=[],
        )
        result = score_job(PASSING_CHEAP_RESULT, no_skills_analysis)
        assert result.component_scores["skills_match"] == 50.0

    def test_location_mismatch_lowers_score_but_does_not_veto(self):
        bad_location_cheap_result = CheapFilterResult(
            passed=True,
            matched_positive_keywords=["it support"],
            min_years_required=0,
            location_ok=False,
        )
        good_location_result = score_job(PASSING_CHEAP_RESULT, ENTRY_LEVEL_ANALYSIS)
        bad_location_result = score_job(bad_location_cheap_result, ENTRY_LEVEL_ANALYSIS)
        assert bad_location_result.vetoed is False
        assert bad_location_result.total_score < good_location_result.total_score

    def test_experience_beyond_max_years_scores_zero_component(self):
        over_experience_cheap_result = CheapFilterResult(
            passed=True,
            matched_positive_keywords=["it support"],
            min_years_required=5,
            location_ok=True,
        )
        result = score_job(over_experience_cheap_result, ENTRY_LEVEL_ANALYSIS, max_experience_years=2)
        assert result.component_scores["experience_match"] == 0.0

    def test_unspecified_experience_treated_favorably(self):
        unspecified_cheap_result = CheapFilterResult(
            passed=True,
            matched_positive_keywords=["it support"],
            min_years_required=None,
            location_ok=True,
        )
        result = score_job(unspecified_cheap_result, ENTRY_LEVEL_ANALYSIS)
        assert result.component_scores["experience_match"] == 85.0


class TestConfigurableWeights:
    def test_custom_weights_change_the_outcome(self):
        # Heavily weighting location alone should make a location mismatch
        # dominate the score.
        bad_location_cheap_result = CheapFilterResult(
            passed=True,
            matched_positive_keywords=["it support"],
            min_years_required=0,
            location_ok=False,
        )
        location_heavy_weights = {
            "entry_level": 0.0,
            "it_relevance": 0.0,
            "skills_match": 0.0,
            "experience_match": 0.0,
            "location": 1.0,
        }
        result = score_job(bad_location_cheap_result, ENTRY_LEVEL_ANALYSIS, weights=location_heavy_weights)
        assert result.total_score == 20  # matches _location_score's mismatch value exactly

    def test_weights_are_normalized_even_if_not_summing_to_one(self):
        # Weights that sum to 2.0 instead of 1.0 should be normalized, not
        # produce a score outside 0-100.
        doubled_weights = {
            "entry_level": 0.60,
            "it_relevance": 0.50,
            "skills_match": 0.40,
            "experience_match": 0.30,
            "location": 0.20,
        }
        result = score_job(PASSING_CHEAP_RESULT, ENTRY_LEVEL_ANALYSIS, weights=doubled_weights)
        assert 0 <= result.total_score <= 100
