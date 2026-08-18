"""
Tests for Phase 12 quality control: JSON shape validation/retry for a single
QC pass, and the generate -> QC -> correct -> revalidate orchestration loop.
"""
from __future__ import annotations

import pytest

from src.ai.base import AIResponseError
from src.ai.schemas import QualityControlResult
from src.resume.schema import MasterResume, Project
from src.quality_control.qc import run_deterministic_checks, run_quality_control, run_quality_control_with_correction
from tests.fakes import FakeAIProvider


def make_sample_resume() -> MasterResume:
    return MasterResume.model_validate(
        {
            "personal": {"full_name": "Test User", "email": "test@example.com"},
            "summary": "Entry-level IT candidate with hands-on project experience.",
            "skills": ["Python", "SQL", "Git"],
            "education": [
                {
                    "id": "edu_01",
                    "institution": "Test University",
                    "credential": "BSc Cybersecurity",
                    "highlights": [{"id": "edu_01_bullet_01", "text": "Studied networking fundamentals.", "skills": []}],
                }
            ],
            "experience": [
                {
                    "id": "exp_01",
                    "company": "Tipaload",
                    "title": "QA Tester",
                    "bullets": [
                        {"id": "exp_01_bullet_01", "text": "Troubleshot two apps in staging and production.", "skills": ["QA testing"]}
                    ],
                }
            ],
            "projects": [],
            "certifications": [],
            "additional": [],
        }
    )


VALID_TAILORING_JSON = {
    "summary": {"action": "keep"},
    "skills": {"action": "keep"},
    "projects": {"action": "keep"},
    "experience": {"action": "keep"},
    "bullet_changes": [],
}

VALID_LETTER_TEXT = (
    "Hello, I'm applying for the IT Support Officer role at Acme. "
    "In my QA Tester role at Tipaload I troubleshot two apps in staging and production. "
) * 10  # comfortably clears the 250-word floor without introducing new facts


class TestDeterministicChecks:
    def test_identical_tailored_resume_has_no_issues(self):
        resume = make_sample_resume()
        issues = run_deterministic_checks(
            resume, resume, "cover letter text", title="IT Support", company="Acme", description="desc"
        )
        assert issues == []

    def test_fabricated_skill_in_cover_letter_is_caught(self):
        resume = make_sample_resume()
        issues = run_deterministic_checks(
            resume,
            resume,
            "I have extensive experience with Kubernetes and AWS.",
            title="IT Support",
            company="Acme",
            description="desc",
        )
        assert any("term(s) not found" in i for i in issues)

    def test_missing_experience_and_projects_is_caught(self):
        resume = make_sample_resume()
        empty_tailored = resume.model_copy(update={"experience": [], "projects": []})
        issues = run_deterministic_checks(
            resume, empty_tailored, "cover letter text", title="IT Support", company="Acme", description="desc"
        )
        assert any("neither experience nor projects" in i for i in issues)

    def test_missing_contact_info_is_caught(self):
        resume = make_sample_resume()
        no_email = resume.model_copy(update={"personal": resume.personal.model_copy(update={"email": ""})})
        issues = run_deterministic_checks(
            resume, no_email, "cover letter text", title="IT Support", company="Acme", description="desc"
        )
        assert any("missing required contact info" in i for i in issues)

    def test_omitting_a_real_experience_entry_is_not_flagged(self):
        # Selecting a subset (dropping an irrelevant real job) is the whole
        # point of tailoring — must never be treated as a problem. Give the
        # tailored copy a project so "at least one of experience/projects"
        # is still satisfied, isolating the thing actually under test.
        resume = make_sample_resume()
        dummy_project = Project(id="project_01", name="CashFlo", technologies=[], bullets=[])
        resume_with_project = resume.model_copy(update={"projects": [dummy_project]})
        subset_tailored = resume_with_project.model_copy(update={"experience": []})
        issues = run_deterministic_checks(
            resume_with_project, subset_tailored, "cover letter text", title="IT Support", company="Acme", description="desc"
        )
        assert issues == []

    def test_matching_dates_and_titles_pass(self):
        resume = make_sample_resume()
        issues = run_deterministic_checks(
            resume, resume, "cover letter text", title="IT Support", company="Acme", description="desc"
        )
        assert issues == []

    def test_mismatched_dates_are_caught(self):
        resume = make_sample_resume()
        tampered_exp = resume.experience[0].model_copy(update={"start_date": "1999-01-01"})
        tampered_tailored = resume.model_copy(update={"experience": [tampered_exp]})
        issues = run_deterministic_checks(
            resume, tampered_tailored, "cover letter text", title="IT Support", company="Acme", description="desc"
        )
        assert any("does not match master resume company/title/dates" in i for i in issues)


class TestRunQualityControlShapeHandling:
    def test_valid_passed_result(self):
        resume = make_sample_resume()
        provider = FakeAIProvider(json_responses=[{"passed": True, "issues": []}])
        result = run_quality_control(
            resume, resume, "cover letter text", title="IT Support", company="Acme", description="desc", provider=provider
        )
        assert result.passed is True
        assert result.issues == []

    def test_valid_failed_result_is_not_retried(self):
        resume = make_sample_resume()
        provider = FakeAIProvider(json_responses=[{"passed": False, "issues": ["fabricated skill: Kubernetes"]}])
        result = run_quality_control(
            resume, resume, "cover letter text", title="IT Support", company="Acme", description="desc", provider=provider
        )
        assert result.passed is False
        assert result.issues == ["fabricated skill: Kubernetes"]
        assert provider.call_count == 1  # a clean "failed" verdict is not itself a retry trigger

    def test_referencing_job_title_and_company_is_not_flagged(self):
        # Regression: the deterministic check must treat the job's own
        # title/company as legitimate context, not fabrication — a cover
        # letter naturally says "applying for the IT Support Officer role
        # at Acme".
        resume = make_sample_resume()
        provider = FakeAIProvider(json_responses=[{"passed": True, "issues": []}])
        result = run_quality_control(
            resume,
            resume,
            "I'm applying for the IT Support Officer role at Acme.",
            title="IT Support Officer",
            company="Acme",
            description="desc",
            provider=provider,
        )
        assert result.passed is True
        assert result.issues == []

    def test_retries_on_malformed_json_then_succeeds(self):
        resume = make_sample_resume()
        provider = FakeAIProvider(json_responses=[{"nonsense": True}, {"passed": True, "issues": []}])
        result = run_quality_control(
            resume, resume, "cover letter text", title="IT Support", company="Acme", description="desc", provider=provider, max_retries=2
        )
        assert result.passed is True
        assert provider.call_count == 2

    def test_fails_safe_after_exhausting_retries(self):
        resume = make_sample_resume()
        provider = FakeAIProvider(json_responses=[{"nonsense": True}, {"nonsense": True}, {"nonsense": True}])
        with pytest.raises(AIResponseError):
            run_quality_control(
                resume, resume, "cover letter text", title="IT Support", company="Acme", description="desc", provider=provider, max_retries=2
            )


class TestCorrectionLoop:
    def test_passes_on_first_attempt(self):
        resume = make_sample_resume()
        tailoring_provider = FakeAIProvider(json_responses=[VALID_TAILORING_JSON])
        letter_provider = FakeAIProvider(text_responses=[VALID_LETTER_TEXT])
        qc_provider = FakeAIProvider(json_responses=[{"passed": True, "issues": []}])

        result = run_quality_control_with_correction(
            resume,
            title="IT Support Officer",
            company="Acme",
            description="desc",
            tailoring_provider=tailoring_provider,
            cover_letter_provider=letter_provider,
            qc_provider=qc_provider,
        )
        assert result.passed is True
        assert result.needs_manual_review is False
        assert result.attempts == 1

    def test_corrects_after_one_qc_failure(self):
        resume = make_sample_resume()
        tailoring_provider = FakeAIProvider(json_responses=[VALID_TAILORING_JSON, VALID_TAILORING_JSON])
        letter_provider = FakeAIProvider(text_responses=[VALID_LETTER_TEXT, VALID_LETTER_TEXT])
        qc_provider = FakeAIProvider(
            json_responses=[
                {"passed": False, "issues": ["wrong company name"]},
                {"passed": True, "issues": []},
            ]
        )

        result = run_quality_control_with_correction(
            resume,
            title="IT Support Officer",
            company="Acme",
            description="desc",
            tailoring_provider=tailoring_provider,
            cover_letter_provider=letter_provider,
            qc_provider=qc_provider,
            max_correction_attempts=2,
        )
        assert result.passed is True
        assert result.attempts == 2

    def test_flags_for_manual_review_after_exhausting_corrections(self):
        resume = make_sample_resume()
        tailoring_provider = FakeAIProvider(json_responses=[VALID_TAILORING_JSON] * 3)
        letter_provider = FakeAIProvider(text_responses=[VALID_LETTER_TEXT] * 3)
        qc_provider = FakeAIProvider(
            json_responses=[{"passed": False, "issues": ["fabricated achievement"]}] * 3
        )

        result = run_quality_control_with_correction(
            resume,
            title="IT Support Officer",
            company="Acme",
            description="desc",
            tailoring_provider=tailoring_provider,
            cover_letter_provider=letter_provider,
            qc_provider=qc_provider,
            max_correction_attempts=2,
        )
        assert result.passed is False
        assert result.needs_manual_review is True
        assert result.attempts == 3
        assert result.qc.issues == ["fabricated achievement"]
