"""
Tests for the deterministic cheap filter, using the exact sample jobs from
the project spec (section 29) so filtering behavior stays verifiably
deterministic as the keyword lists evolve.
"""
from __future__ import annotations

from src.job_filter.cheap_filter import run_cheap_filter
from src.job_filter.experience import extract_min_years_required

SAMPLE_JOBS = {
    "entry_level_it_support": {
        "title": "IT Support Officer",
        "description": "Entry-level role on our service desk. No experience required. "
        "Troubleshoot hardware/software issues for staff. Windows, Active Directory a plus.",
        "location": "Sydney NSW",
    },
    "senior_systems_engineer": {
        "title": "Senior Systems Engineer",
        "description": "We need a Senior Systems Engineer with 5+ years of experience "
        "managing enterprise infrastructure and leading a team of engineers.",
        "location": "Sydney NSW",
    },
    "graduate_it_support": {
        "title": "Graduate IT Support",
        "description": "Join our graduate program as a Graduate IT Support team member. "
        "No prior professional experience necessary — full training provided.",
        "location": "Melbourne VIC",
    },
    "service_desk_1_year": {
        "title": "Service Desk Analyst",
        "description": "Service Desk Analyst role requiring 1 year of experience in a "
        "similar help desk position. Ticket management, troubleshooting.",
        "location": "Remote Australia",
    },
    "it_manager": {
        "title": "IT Manager",
        "description": "IT Manager needed to lead our technology department and manage "
        "a team of support staff. Minimum 5 years of management experience required.",
        "location": "Sydney NSW",
    },
    "junior_application_support": {
        "title": "Junior Application Support",
        "description": "Junior Application Support analyst to help maintain internal "
        "business applications. 0-1 years experience, graduate-friendly.",
        "location": "Sydney NSW",
    },
    "software_engineer_5_years": {
        "title": "Software Engineer",
        "description": "Software Engineer position requiring 5 years of professional "
        "experience building scalable backend systems.",
        "location": "Sydney NSW",
    },
}


class TestSampleJobsFromSpec:
    def test_entry_level_it_support_passes(self):
        job = SAMPLE_JOBS["entry_level_it_support"]
        result = run_cheap_filter(job["title"], job["description"], job["location"])
        assert result.passed is True
        assert result.rejection_category is None

    def test_senior_systems_engineer_rejected(self):
        job = SAMPLE_JOBS["senior_systems_engineer"]
        result = run_cheap_filter(job["title"], job["description"], job["location"])
        assert result.passed is False
        assert result.rejection_category == "SENIORITY"

    def test_graduate_it_support_rejected(self):
        # Deliberate policy change, 2026-09-16: grad schemes are explicitly
        # out of scope for the target band (L0/1 help desk / IT support /
        # service desk) even though they're also "entry-level IT" — see
        # NEGATIVE_ROLE_TYPE_KEYWORDS in src/job_filter/keywords.py. This
        # sample used to assert passed is True; it now asserts the
        # opposite on purpose, not because the old assertion was wrong.
        job = SAMPLE_JOBS["graduate_it_support"]
        result = run_cheap_filter(job["title"], job["description"], job["location"])
        assert result.passed is False
        assert result.rejection_category == "ROLE_TYPE"

    def test_service_desk_1_year_passes(self):
        job = SAMPLE_JOBS["service_desk_1_year"]
        result = run_cheap_filter(job["title"], job["description"], job["location"])
        assert result.passed is True

    def test_it_manager_rejected(self):
        job = SAMPLE_JOBS["it_manager"]
        result = run_cheap_filter(job["title"], job["description"], job["location"])
        assert result.passed is False
        assert result.rejection_category == "SENIORITY"

    def test_junior_application_support_passes(self):
        job = SAMPLE_JOBS["junior_application_support"]
        result = run_cheap_filter(job["title"], job["description"], job["location"])
        assert result.passed is True

    def test_software_engineer_5_years_rejected(self):
        # Side effect of the 2026-09-16 ROLE_TYPE addition: "Software
        # Engineer" is now hard-rejected as the wrong role type before the
        # experience check even runs, so this sample's rejection_category
        # changed from EXPERIENCE to ROLE_TYPE. passed is still False either
        # way — this job was always going to be rejected — but the reason
        # given is now more precise (wrong role, not just too many years).
        job = SAMPLE_JOBS["software_engineer_5_years"]
        result = run_cheap_filter(job["title"], job["description"], job["location"])
        assert result.passed is False
        assert result.rejection_category == "ROLE_TYPE"


class TestHardVetoOverridesSkillMatch:
    def test_senior_job_with_perfect_keyword_match_still_rejected(self):
        # A senior job stuffed with every positive keyword must still be
        # rejected — a high skills/keyword match must never override a
        # seniority veto (spec section 16).
        title = "Senior IT Support Officer / Service Desk Lead"
        description = (
            "Senior-level Service Desk Officer / Team Lead role. Help Desk, "
            "Desktop Support, Technical Support, ICT Support, Junior IT, Graduate IT "
            "all reporting to this Manager position. 5+ years required."
        )
        result = run_cheap_filter(title, description, "Sydney NSW")
        assert result.passed is False
        assert result.rejection_category == "SENIORITY"


class TestExperienceExtraction:
    def test_extracts_plus_years(self):
        assert extract_min_years_required("Requires 3+ years of experience") == 3

    def test_extracts_range(self):
        assert extract_min_years_required("3-5 years in a similar role") == 3

    def test_extracts_minimum_phrasing(self):
        assert extract_min_years_required("Minimum of 4 years experience required") == 4

    def test_extracts_years_of_experience_phrasing(self):
        assert extract_min_years_required("2 years of experience in IT support") == 2

    def test_returns_none_when_unspecified(self):
        assert extract_min_years_required("No experience required, full training given") is None

    def test_takes_minimum_of_multiple_mentions(self):
        text = "1 year of experience preferred; up to 5+ years for senior candidates"
        assert extract_min_years_required(text) == 1


class TestConfigurableExperienceCap:
    def test_two_years_passes_with_default_cap(self):
        result = run_cheap_filter(
            "IT Support Officer",
            "Requires 2 years of experience troubleshooting desktop issues.",
            "Sydney NSW",
            max_experience_years=2,
        )
        assert result.passed is True

    def test_three_years_rejected_with_default_cap(self):
        result = run_cheap_filter(
            "IT Support Officer",
            "Requires 3 years of experience troubleshooting desktop issues.",
            "Sydney NSW",
            max_experience_years=2,
        )
        assert result.passed is False
        assert result.rejection_category == "EXPERIENCE"

    def test_cap_is_configurable(self):
        # With a higher configured cap, a 3-year requirement should pass.
        result = run_cheap_filter(
            "IT Support Officer",
            "Requires 3 years of experience troubleshooting desktop issues.",
            "Sydney NSW",
            max_experience_years=4,
        )
        assert result.passed is True


class TestTierAndLevelSeniorityMarkers:
    """
    Real gap found in data/jobs.db on 2026-09-16: "Technical Support
    Engineer - Tier 3" (job #2003) passed the old filter and reached
    READY_TO_APPLY with fit_score 85 — a Tier 3 role, not the target L0/1
    band. NEGATIVE_SENIORITY_KEYWORDS previously only caught "l3"/"level 3",
    not "tier 3", "engineer ii/iii", or "2nd/3rd line" phrasing.
    """

    def test_tier_3_support_engineer_rejected(self):
        result = run_cheap_filter(
            "Technical Support Engineer - Tier 3",
            "Provide technical support to enterprise customers.",
            "Sydney NSW",
        )
        assert result.passed is False
        assert result.rejection_category == "SENIORITY"

    def test_engineer_ii_iii_rejected(self):
        result = run_cheap_filter(
            "IT Support Engineer II/III",
            "Provide IT support to staff across the business.",
            "Sydney NSW",
        )
        assert result.passed is False
        assert result.rejection_category == "SENIORITY"

    def test_2nd_line_support_rejected(self):
        result = run_cheap_filter(
            "2nd Line Support Technician",
            "Handle escalated support tickets from the service desk.",
            "Sydney NSW",
        )
        assert result.passed is False
        assert result.rejection_category == "SENIORITY"


class TestNoPositiveMatch:
    def test_unrelated_job_rejected(self):
        result = run_cheap_filter(
            "Warehouse Forklift Operator",
            "Operate forklifts and manage warehouse inventory.",
            "Sydney NSW",
        )
        assert result.passed is False
        assert result.rejection_category in {"NON_IT", "NO_POSITIVE_MATCH"}
