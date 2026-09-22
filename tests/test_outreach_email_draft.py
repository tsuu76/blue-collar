"""
Tests for outreach email drafting, validation and DRAFT storage.

No network and no Ollama — FakeAIProvider supplies scripted email text. All
companies, postings and email bodies here are synthetic fixtures.
"""
from __future__ import annotations

import pytest

from src.ai.base import AIResponseError
from src.ai.schemas import OutreachAnalysis
from src.database.db import get_connection, init_db
from src.database.models import OutreachStatus
from src.database.outreach_repo import (
    approve_message,
    get_message,
    insert_company,
    list_messages_for_company,
    set_do_not_contact,
)
from src.outreach.email_draft import (
    EmailDraft,
    build_prompt,
    draft_outreach_email,
    generate_outreach_email,
    profile_links,
    split_subject_body,
    validate_outreach_email,
)
from src.outreach.personalization import PersonalizationResult
from src.outreach.research import CompanyResearch, PostingResearch
from src.resume.schema import Bullet, Education, MasterResume, Personal, Project
from tests.fakes import FakeAIProvider


@pytest.fixture()
def resume() -> MasterResume:
    return MasterResume(
        personal=Personal(
            full_name="Test Candidate",
            email="test@example.invalid",
            github="github.com/testcandidate",
        ),
        summary="First-year IT student studying cybersecurity.",
        skills=["Python", "SQL", "Git"],
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
                bullets=[Bullet(id="proj_01_b1", text="Built a small expense tracker in Python.", skills=["Python"])],
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
                description="Troubleshoot customer issues. Python and SQL are used daily.",
            )
        ],
    )


@pytest.fixture()
def personalization() -> PersonalizationResult:
    return PersonalizationResult(
        analysis=OutreachAnalysis(
            recurring_skills=["Python", "SQL"],
            tools_and_technologies=["Python"],
            responsibilities=["Troubleshoot customer issues"],
            experience_requirements=["Entry level"],
            terminology=["Service Desk"],
            candidate_overlap=["Python", "SQL"],
            notes="They hire support-focused technical staff.",
        ),
        dropped_company_terms=[],
        dropped_candidate_claims=[],
    )


# A body that passes every check: right length, no clichés, only real skills,
# only things the postings actually said.
GOOD_BODY = " ".join(
    [
        "Hi there,",
        "I had a look at what Example Co is hiring for at the moment, and the Service Desk",
        "Analyst role stood out because so much of it is troubleshooting with Python and SQL",
        "in the mix. Those are the two things I have spent the most time on so far.",
        "I am a first-year IT student. Most of my practice has come from building TestApp,",
        "a small expense tracker I wrote in Python with a SQL database behind it, plus the",
        "networking side of my course. I am not going to pretend that adds up to industry",
        "experience, but I can troubleshoot patiently and I pick things up quickly.",
        "I am looking for a first proper opportunity, so anything entry level, junior or",
        "support based would be great. If there is someone better placed for me to talk to,",
        "I would appreciate a pointer in their direction.",
        "Thanks for your time,",
        "Test",
    ]
)

GOOD_EMAIL = f"Subject: Student interested in entry-level work\n\n{GOOD_BODY}\nGitHub: github.com/testcandidate"


def _valid_draft() -> EmailDraft:
    return split_subject_body(GOOD_EMAIL)


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "outreach.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture()
def company(conn):
    company_id = insert_company(
        conn,
        {
            "name": "Example Co",
            "website": "https://example.invalid",
            "contact_email": "careers@example.invalid",
            "platform": "greenhouse",
            "identifier": "exampleco",
        },
    )
    conn.commit()
    return {"id": company_id, "name": "Example Co", "contact_email": "careers@example.invalid"}


class TestSplitSubjectBody:
    def test_splits_subject_and_body(self):
        draft = split_subject_body("Subject: Hello there\n\nBody line one.\nBody line two.")
        assert draft.subject == "Hello there"
        assert draft.body == "Body line one.\nBody line two."

    def test_is_case_insensitive(self):
        assert split_subject_body("SUBJECT: Hi\n\nBody.").subject == "Hi"

    def test_strips_surrounding_quotes(self):
        assert split_subject_body('Subject: "Hi there"\n\nBody.').subject == "Hi there"

    def test_missing_subject_leaves_it_empty(self):
        """A missing subject must not be invented or stolen from the body."""
        draft = split_subject_body("Just a body with no subject line.")
        assert draft.subject == ""
        assert draft.body == "Just a body with no subject line."

    def test_empty_input(self):
        draft = split_subject_body("")
        assert draft.subject == ""
        assert draft.body == ""


class TestProfileLinks:
    def test_includes_only_configured_links(self, resume):
        assert profile_links(resume) == ["GitHub: github.com/testcandidate"]

    def test_includes_linkedin_and_portfolio_when_set(self, resume):
        resume.personal.linkedin = "linkedin.com/in/testcandidate"
        resume.personal.portfolio = "testcandidate.example.invalid"
        links = profile_links(resume)
        assert links[0].startswith("LinkedIn:")
        assert any(link.startswith("Portfolio:") for link in links)

    def test_no_links_configured_returns_empty(self):
        assert profile_links(MasterResume(personal=Personal(full_name="X"))) == []


class TestBuildPrompt:
    def test_includes_company_evidence_and_verified_overlap(self, resume, research, personalization):
        prompt = build_prompt(resume, research, personalization)
        assert "Example Co" in prompt
        assert "Service Desk Analyst" in prompt
        assert "TestApp" in prompt

    def test_tells_the_model_not_to_apply_for_a_job(self, resume, research, personalization):
        assert "NOT an application for an advertised job" in build_prompt(resume, research, personalization)

    def test_says_not_to_mention_links_when_none_configured(self, research, personalization):
        bare = MasterResume(personal=Personal(full_name="X"), skills=["Python"])
        assert "do not mention any links" in build_prompt(bare, research, personalization)


class TestValidation:
    def test_good_email_passes(self, resume, research, personalization):
        assert validate_outreach_email(_valid_draft(), resume, research, personalization) == []

    def test_rejects_empty_body(self, resume, research, personalization):
        problems = validate_outreach_email(EmailDraft("Subject", "   "), resume, research, personalization)
        assert problems == ["Email body is empty"]

    def test_rejects_missing_subject(self, resume, research, personalization):
        draft = EmailDraft("", GOOD_BODY)
        assert any("no subject" in p for p in validate_outreach_email(draft, resume, research, personalization))

    def test_rejects_overlong_subject(self, resume, research, personalization):
        draft = EmailDraft("A subject line that just keeps going on and on and on forever", GOOD_BODY)
        assert any("Subject line is too long" in p for p in validate_outreach_email(draft, resume, research, personalization))

    def test_rejects_too_short(self, resume, research, personalization):
        draft = EmailDraft("Hello", "Hi there, I would like a job. Thanks.")
        assert any("too short" in p for p in validate_outreach_email(draft, resume, research, personalization))

    def test_rejects_too_long(self, resume, research, personalization):
        draft = EmailDraft("Hello", " ".join(["word"] * 500))
        assert any("too long" in p for p in validate_outreach_email(draft, resume, research, personalization))

    @pytest.mark.parametrize(
        "cliche",
        [
            "I am writing to express my interest in your company.",
            "I would leverage my skills to help the team.",
            "I bring a unique skill set to the table.",
            "I am excited about the opportunity to join you.",
            "I hope this email finds you well.",
        ],
    )
    def test_rejects_cliches(self, resume, research, personalization, cliche):
        draft = EmailDraft("Hello there", f"{cliche} {GOOD_BODY}")
        problems = validate_outreach_email(draft, resume, research, personalization)
        assert any("generic/AI-sounding phrasing" in p for p in problems)

    @pytest.mark.parametrize(
        "phrase",
        [
            "I am applying for the Service Desk Analyst position.",
            "Please consider my application for the role.",
            "I saw your job posting for a support engineer.",
        ],
    )
    def test_rejects_pretending_to_apply_for_a_specific_job(
        self, resume, research, personalization, phrase
    ):
        draft = EmailDraft("Hello there", f"{phrase} {GOOD_BODY}")
        problems = validate_outreach_email(draft, resume, research, personalization)
        assert any("general outreach" in p for p in problems)

    def test_rejects_placeholders(self, resume, research, personalization):
        draft = EmailDraft("Hello there", f"Hi [Company], {GOOD_BODY}")
        problems = validate_outreach_email(draft, resume, research, personalization)
        assert any("placeholder" in p for p in problems)

    def test_rejects_fabricated_skill(self, resume, research, personalization):
        """The critical case: a technology in neither the resume nor the
        company's postings must be caught."""
        draft = EmailDraft("Hello there", f"{GOOD_BODY} I have also worked with Kubernetes and Terraform.")
        problems = validate_outreach_email(draft, resume, research, personalization)
        assert any("fabricated skill" in p for p in problems)

    def test_rejects_fabricated_number(self, resume, research, personalization):
        draft = EmailDraft("Hello there", f"{GOOD_BODY} I have 7 years of experience.")
        problems = validate_outreach_email(draft, resume, research, personalization)
        assert any("fabricated metric" in p for p in problems)

    def test_allows_the_companys_own_terminology(self, resume, research, personalization):
        """Using the company's real words is personalization, not fabrication."""
        draft = EmailDraft("Hello there", GOOD_BODY.replace("Service Desk", "Service Desk"))
        assert validate_outreach_email(draft, resume, research, personalization) == []

    def test_rejects_invented_claim_about_the_company(self, resume, research, personalization):
        draft = EmailDraft("Hello there", f"{GOOD_BODY} I admire your work in Fintech across Melbourne.")
        problems = validate_outreach_email(draft, resume, research, personalization)
        assert any("fabricated skill or claim about the company" in p for p in problems)


class TestGenerate:
    def test_returns_valid_draft(self, resume, research, personalization):
        provider = FakeAIProvider(text_responses=[GOOD_EMAIL])
        draft = generate_outreach_email(resume, research, personalization, provider=provider)
        assert draft.subject == "Student interested in entry-level work"
        assert "TestApp" in draft.body

    def test_retries_after_a_bad_draft(self, resume, research, personalization):
        bad = "Subject: Hi\n\nI am writing to express my interest in your company."
        provider = FakeAIProvider(text_responses=[bad, GOOD_EMAIL])
        draft = generate_outreach_email(resume, research, personalization, provider=provider, max_retries=2)
        assert draft.subject == "Student interested in entry-level work"
        assert provider.text_call_count == 2

    def test_raises_when_every_attempt_fabricates(self, resume, research, personalization):
        bad = f"Subject: Hi there\n\n{GOOD_BODY} I am also skilled in Kubernetes."
        provider = FakeAIProvider(text_responses=[bad] * 3)
        with pytest.raises(AIResponseError, match="fabricated"):
            generate_outreach_email(resume, research, personalization, provider=provider, max_retries=2)


class TestDraftAndStore:
    def test_stores_a_draft(self, conn, company, resume, research, personalization):
        provider = FakeAIProvider(text_responses=[GOOD_EMAIL])
        result = draft_outreach_email(conn, company, research, resume, personalization, provider=provider)
        conn.commit()

        assert result.ok is True
        row = get_message(conn, result.message_id)
        assert row["status"] == OutreachStatus.DRAFT
        assert row["subject"] == "Student interested in entry-level work"
        assert row["recipient_email"] == "careers@example.invalid"
        assert row["sent_at"] is None

    def test_rejected_draft_is_not_stored(self, conn, company, resume, research, personalization):
        """A draft that fails validation is rejected outright, never stored
        with invented content patched in."""
        bad = f"Subject: Hi there\n\n{GOOD_BODY} I am also certified in Kubernetes."
        provider = FakeAIProvider(text_responses=[bad] * 4)
        result = draft_outreach_email(conn, company, research, resume, personalization, provider=provider)
        conn.commit()

        assert result.ok is False
        assert result.message_id is None
        assert "rejected" in result.skipped_reason
        assert list_messages_for_company(conn, company["id"]) == []

    def test_skips_do_not_contact_company(self, conn, company, resume, research, personalization):
        set_do_not_contact(conn, company["id"], "opted out")
        conn.commit()
        provider = FakeAIProvider(text_responses=[GOOD_EMAIL])
        result = draft_outreach_email(conn, company, research, resume, personalization, provider=provider)

        assert result.ok is False
        assert "do-not-contact" in result.skipped_reason
        assert provider.text_call_count == 0

    def test_skips_when_a_message_is_already_pending(self, conn, company, resume, research, personalization):
        provider = FakeAIProvider(text_responses=[GOOD_EMAIL, GOOD_EMAIL])
        draft_outreach_email(conn, company, research, resume, personalization, provider=provider)
        conn.commit()
        second = draft_outreach_email(conn, company, research, resume, personalization, provider=provider)

        assert second.ok is False
        assert "already has a draft" in second.skipped_reason
        assert len(list_messages_for_company(conn, company["id"])) == 1

    def test_skips_when_there_are_no_postings(self, conn, company, resume, personalization):
        empty = CompanyResearch(company="Example Co", error="no supported ATS board configured")
        provider = FakeAIProvider(text_responses=[GOOD_EMAIL])
        result = draft_outreach_email(conn, company, empty, resume, personalization, provider=provider)

        assert result.ok is False
        assert "no real postings" in result.skipped_reason
        assert provider.text_call_count == 0

    def test_skips_when_there_is_no_verified_overlap(self, conn, company, resume, research):
        """No genuine common ground means no email, rather than a vague one."""
        empty_overlap = PersonalizationResult(
            analysis=OutreachAnalysis(recurring_skills=["Python"], candidate_overlap=[]),
            dropped_company_terms=[],
            dropped_candidate_claims=["Java"],
        )
        provider = FakeAIProvider(text_responses=[GOOD_EMAIL])
        result = draft_outreach_email(conn, company, research, resume, empty_overlap, provider=provider)

        assert result.ok is False
        assert "no verified overlap" in result.skipped_reason
        assert provider.text_call_count == 0

    def test_stored_draft_is_not_approved_or_sent(self, conn, company, resume, research, personalization):
        provider = FakeAIProvider(text_responses=[GOOD_EMAIL])
        result = draft_outreach_email(conn, company, research, resume, personalization, provider=provider)
        conn.commit()

        row = get_message(conn, result.message_id)
        assert row["status"] == OutreachStatus.DRAFT
        assert row["approved_at"] is None
        assert row["sent_at"] is None
        assert row["send_attempts"] == 0

        # Drafting alone never makes a message approvable — the deterministic
        # quality gate has to record a PASS first (see
        # src/outreach/quality_gate.py). An ungated draft is refused.
        assert row["gate_passed"] is None
        assert approve_message(conn, result.message_id) is False
        assert get_message(conn, result.message_id)["status"] == OutreachStatus.DRAFT
