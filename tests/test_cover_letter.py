"""
Tests for Phase 11 cover letter generation: word count, cliché/generic-AI-
language detection, fabrication guards, and retry-then-fail-safe behavior.
"""
from __future__ import annotations

import pytest

from src.ai.base import AIResponseError
from src.cover_letter.generator import generate_cover_letter, validate_cover_letter
from src.resume.schema import MasterResume
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
            "projects": [
                {
                    "id": "project_01",
                    "name": "CashFlo",
                    "technologies": ["React", "SQLite"],
                    "bullets": [{"id": "project_01_bullet_01", "text": "Built a full-stack finance app.", "skills": ["React", "SQLite"]}],
                }
            ],
            "certifications": [],
            "additional": [],
        }
    )


def make_valid_letter(word_count: int = 260) -> str:
    # Build a letter that stays grounded in the resume facts and hits a
    # target word count, avoiding both clichés and fabricated content.
    base = (
        "Hello, I'm applying for the IT Support Officer role at Acme. "
        "In my QA Tester role at Tipaload I troubleshot two apps in staging and production, "
        "which gave me practical experience diagnosing real issues under time pressure. "
        "I also built CashFlo, a full-stack finance app, where I worked with Python, SQL, and Git "
        "throughout the project. My studies in Cybersecurity at Test University included networking "
        "fundamentals, which I think would transfer well to a service desk environment. "
    )
    filler = "I am glad to discuss this role further and thank you for your consideration. "
    text = base
    while len(text.split()) < word_count:
        text += filler
    return text.strip()


class TestWordCountValidation:
    def test_too_short_is_flagged(self):
        resume = make_sample_resume()
        problems = validate_cover_letter(
            "Short letter.", resume, title="IT Support Officer", company="Acme", description="desc"
        )
        assert any("too short" in p for p in problems)

    def test_too_long_is_flagged(self):
        resume = make_sample_resume()
        long_text = make_valid_letter(500)
        problems = validate_cover_letter(long_text, resume, title="IT Support Officer", company="Acme", description="desc")
        assert any("too long" in p for p in problems)

    def test_within_range_is_not_flagged_for_length(self):
        resume = make_sample_resume()
        text = make_valid_letter(300)
        problems = validate_cover_letter(text, resume, title="IT Support Officer", company="Acme", description="desc")
        length_problems = [p for p in problems if "too short" in p or "too long" in p]
        assert length_problems == []

    def test_empty_text_flagged(self):
        resume = make_sample_resume()
        problems = validate_cover_letter("", resume, title="IT Support Officer", company="Acme", description="desc")
        assert problems == ["Cover letter text is empty"]


class TestClicheDetection:
    def test_cliche_phrase_is_flagged(self):
        resume = make_sample_resume()
        text = make_valid_letter(280) + " I am thrilled about this opportunity."
        problems = validate_cover_letter(text, resume, title="IT Support Officer", company="Acme", description="desc")
        assert any("cliché" in p for p in problems)

    def test_clean_letter_has_no_cliche_flag(self):
        resume = make_sample_resume()
        text = make_valid_letter(280)
        problems = validate_cover_letter(text, resume, title="IT Support Officer", company="Acme", description="desc")
        cliche_problems = [p for p in problems if "cliché" in p]
        assert cliche_problems == []

    def test_confident_closing_is_not_a_cliche(self):
        # "I'm confident that my skills..." is normal, professional cover
        # letter phrasing, not an AI-tell — must not be blocked.
        resume = make_sample_resume()
        text = make_valid_letter(280) + " I'm confident that my background would be a good match for this role."
        problems = validate_cover_letter(text, resume, title="IT Support Officer", company="Acme", description="desc")
        cliche_problems = [p for p in problems if "cliché" in p]
        assert cliche_problems == []


class TestFabricationGuardOnCoverLetter:
    def test_fabricated_metric_is_flagged(self):
        resume = make_sample_resume()
        text = make_valid_letter(280) + " I improved system uptime by 45%."
        problems = validate_cover_letter(text, resume, title="IT Support Officer", company="Acme", description="desc")
        assert any("fabricated metric" in p for p in problems)

    def test_fabricated_technology_is_flagged(self):
        resume = make_sample_resume()
        text = make_valid_letter(280) + " I have also worked extensively with Kubernetes and AWS."
        problems = validate_cover_letter(text, resume, title="IT Support Officer", company="Acme", description="desc")
        assert any("fabricated skill" in p for p in problems)

    def test_hyphenated_compound_of_a_known_term_is_not_flagged(self):
        # "SQLite-based" is a hyphenated compound built from a real
        # technology (SQLite) already in the resume — the "-based" suffix
        # shouldn't make it look like a brand new fabricated term.
        resume = make_sample_resume()
        text = make_valid_letter(280) + " I built a SQLite-based storage layer for the project."
        problems = validate_cover_letter(text, resume, title="IT Support Officer", company="Acme", description="desc")
        fabrication_problems = [p for p in problems if "fabricated" in p]
        assert fabrication_problems == []

    def test_referencing_job_title_terms_is_allowed(self):
        resume = make_sample_resume()
        text = make_valid_letter(280)
        problems = validate_cover_letter(
            text,
            resume,
            title="IT Support Officer",
            company="Acme",
            description="Join our Service Desk team supporting Windows and Active Directory.",
        )
        # Referencing the job's own terminology (even words not in the
        # resume, like "Windows") must not itself be flagged — the letter
        # text above never actually uses those words, so this just proves
        # the base valid letter still comes back clean.
        fabrication_problems = [p for p in problems if "fabricated" in p]
        assert fabrication_problems == []


class TestGenerateCoverLetterRetry:
    def test_succeeds_on_first_valid_response(self):
        resume = make_sample_resume()
        provider = FakeAIProvider(text_responses=[make_valid_letter(280)])
        result = generate_cover_letter(resume, title="IT Support Officer", company="Acme", description="desc", provider=provider)
        assert len(result.split()) >= 250

    def test_retries_after_cliche_then_succeeds(self):
        resume = make_sample_resume()
        bad = make_valid_letter(280) + " I am passionate about this dynamic environment."
        good = make_valid_letter(280)
        provider = FakeAIProvider(text_responses=[bad, good])
        result = generate_cover_letter(
            resume, title="IT Support Officer", company="Acme", description="desc", provider=provider, max_retries=2
        )
        assert result == good

    def test_fails_safe_when_always_too_short(self):
        resume = make_sample_resume()
        provider = FakeAIProvider(text_responses=["Too short.", "Still short.", "Still not enough."])
        with pytest.raises(AIResponseError):
            generate_cover_letter(
                resume, title="IT Support Officer", company="Acme", description="desc", provider=provider, max_retries=2
            )
