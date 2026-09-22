"""
Tests for the deterministic outreach quality gate.

No network, no AI, no email — the gate makes none of those calls, and these
tests make none either. Every company, posting, resume and email body is a
synthetic fixture written for this file.

The structure mirrors the gate's own checks: one class per requirement, plus
enforcement tests proving a FAIL can never become eligible for sending.
"""
from __future__ import annotations

import json

import pytest

from src.config import settings
from src.database.db import get_connection, init_db
from src.database.models import OutreachStatus
from src.database.outreach_repo import (
    approve_message,
    get_message,
    insert_company,
    insert_message,
    list_approved_messages,
    mark_message_sent,
    save_gate_result,
    set_do_not_contact,
    update_company_contact_email,
    update_message_content,
)
from src.outreach import quality_gate
from src.outreach.quality_gate import (
    FAIL,
    PASS,
    Check,
    GateResult,
    evaluate_message,
    gate_and_record,
    is_eligible,
)
from src.resume.schema import Bullet, Education, MasterResume, Personal, Project

POSTING_DESCRIPTION = (
    "Troubleshoot customer issues on our service desk. Python and SQL are used daily "
    "by the team. Entry level applicants welcome."
)

SNAPSHOT = {
    "company": "Example Co",
    "platform": "greenhouse",
    "identifier": "exampleco",
    "postings": [
        {
            "title": "Service Desk Analyst",
            "url": "https://example.invalid/1",
            "location": "Sydney",
            "description": POSTING_DESCRIPTION,
        }
    ],
}

ANALYSIS = {
    "analysis": {
        "recurring_skills": ["Python", "SQL"],
        "tools_and_technologies": ["Python"],
        "responsibilities": ["Troubleshoot customer issues"],
        "experience_requirements": ["Entry level"],
        "terminology": ["Service Desk"],
        "candidate_overlap": ["Python", "SQL"],
        "notes": "They hire support-focused technical staff.",
    },
    "dropped_company_terms": [],
    "dropped_candidate_claims": [],
}

GOOD_BODY = " ".join(
    [
        "Hi there,",
        "I had a look at what Example Co is hiring for and the Service Desk Analyst role",
        "stood out, because so much of it is troubleshooting with Python and SQL in the mix.",
        "Those are the two things I have spent the most time on so far.",
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

GOOD_SUBJECT = "Student interested in entry-level work"


@pytest.fixture()
def resume() -> MasterResume:
    return MasterResume(
        personal=Personal(full_name="Test Candidate", email="test@example.invalid"),
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
                bullets=[
                    Bullet(id="proj_01_b1", text="Built a small expense tracker in Python.", skills=["Python"])
                ],
            )
        ],
    )


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "gate.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture()
def company_id(conn):
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
    return company_id


def _message(conn, company_id, **overrides) -> int:
    message_id = insert_message(
        conn,
        {
            "company_id": company_id,
            "recipient_email": "careers@example.invalid",
            "subject": GOOD_SUBJECT,
            "body": GOOD_BODY,
            "analysis": ANALYSIS,
            "research_snapshot": SNAPSHOT,
            **overrides,
        },
    )
    conn.commit()
    return message_id


def _check(result: GateResult, name: str):
    return next(c for c in result.checks if c.name == name)


class TestPassingMessage:
    def test_a_good_message_passes(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id), resume)
        assert result.passed is True, result.reasons
        assert result.verdict == PASS
        assert result.reasons == []

    def test_every_check_runs(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id), resume)
        assert {c.name for c in result.checks} == set(Check.ALL)

    def test_result_is_serializable(self, conn, company_id, resume):
        payload = json.loads(json.dumps(evaluate_message(conn, _message(conn, company_id), resume).to_dict()))
        assert payload["verdict"] == PASS
        assert len(payload["checks"]) == len(Check.ALL)

    def test_unknown_message_raises(self, conn, resume):
        with pytest.raises(ValueError):
            evaluate_message(conn, 9999, resume)

    def test_gate_makes_no_ai_or_network_call(self):
        """The gate is deterministic by construction — it imports no AI
        provider, no HTTP client and no mail library."""
        import pathlib

        source = pathlib.Path("src/outreach/quality_gate.py").read_text()
        for token in ("get_ai_provider", "requests", "smtplib", "polite_get", "generate("):
            assert token not in source, f"quality_gate references {token!r}"


class TestDoNotContact:
    def test_fails_when_company_opted_out(self, conn, company_id, resume):
        message_id = _message(conn, company_id)
        set_do_not_contact(conn, company_id, "asked not to be contacted")
        conn.commit()

        result = evaluate_message(conn, message_id, resume)
        assert result.verdict == FAIL
        assert _check(result, Check.NOT_DO_NOT_CONTACT).passed is False
        assert "asked not to be contacted" in _check(result, Check.NOT_DO_NOT_CONTACT).reason


class TestAlreadyContacted:
    def test_fails_when_company_already_emailed(self, conn, company_id, resume):
        first = _message(conn, company_id)
        save_gate_result(conn, first, passed=True, reasons=[])
        approve_message(conn, first)
        mark_message_sent(conn, first)
        second = _message(conn, company_id)
        conn.commit()

        result = evaluate_message(conn, second, resume)
        assert _check(result, Check.NOT_ALREADY_CONTACTED).passed is False


class TestDuplicatePending:
    def test_fails_when_another_draft_exists(self, conn, company_id, resume):
        _message(conn, company_id)
        second = _message(conn, company_id)
        result = evaluate_message(conn, second, resume)
        assert _check(result, Check.NO_DUPLICATE_PENDING).passed is False

    def test_a_message_does_not_flag_itself(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id), resume)
        assert _check(result, Check.NO_DUPLICATE_PENDING).passed is True


class TestRealPostings:
    def test_fails_without_postings(self, conn, company_id, resume):
        message_id = _message(conn, company_id, research_snapshot={"company": "Example Co", "postings": []})
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.REAL_POSTINGS).passed is False

    def test_fails_with_no_snapshot_at_all(self, conn, company_id, resume):
        message_id = _message(conn, company_id, research_snapshot=None)
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.REAL_POSTINGS).passed is False


class TestContactEmail:
    def test_fails_without_a_recipient(self, conn, company_id, resume):
        message_id = _message(conn, company_id, recipient_email="")
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.VERIFIED_CONTACT_EMAIL).passed is False

    def test_fails_when_company_has_no_verified_address(self, conn, company_id, resume):
        message_id = _message(conn, company_id)
        update_company_contact_email(conn, company_id, "")
        conn.commit()
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.VERIFIED_CONTACT_EMAIL).passed is False

    def test_fails_when_recipient_was_redirected(self, conn, company_id, resume):
        """An edited recipient that no longer matches the company's verified
        address must not slip through."""
        message_id = _message(conn, company_id, recipient_email="someone.else@elsewhere.invalid")
        result = evaluate_message(conn, message_id, resume)
        check = _check(result, Check.VERIFIED_CONTACT_EMAIL)
        assert check.passed is False
        assert "elsewhere.invalid" in check.reason

    def test_case_difference_is_not_a_redirect(self, conn, company_id, resume):
        message_id = _message(conn, company_id, recipient_email="Careers@Example.invalid")
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.VERIFIED_CONTACT_EMAIL).passed is True


class TestVerifiedOverlap:
    def test_fails_with_empty_overlap(self, conn, company_id, resume):
        analysis = {**ANALYSIS, "analysis": {**ANALYSIS["analysis"], "candidate_overlap": []}}
        message_id = _message(conn, company_id, analysis=analysis)
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.VERIFIED_OVERLAP).passed is False

    def test_fails_with_no_analysis_at_all(self, conn, company_id, resume):
        message_id = _message(conn, company_id, analysis=None)
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.VERIFIED_OVERLAP).passed is False


class TestCompanySkillsAreReal:
    def test_fails_when_a_skill_is_not_in_the_postings(self, conn, company_id, resume):
        """A technology attributed to the company that their postings never
        mention is a fabricated claim about them."""
        analysis = {
            **ANALYSIS,
            "analysis": {**ANALYSIS["analysis"], "recurring_skills": ["Python", "Kubernetes"]},
        }
        message_id = _message(conn, company_id, analysis=analysis)
        result = evaluate_message(conn, message_id, resume)
        check = _check(result, Check.COMPANY_SKILLS_REAL)
        assert check.passed is False
        assert "Kubernetes" in check.reason

    def test_fails_for_invented_tooling(self, conn, company_id, resume):
        analysis = {
            **ANALYSIS,
            "analysis": {**ANALYSIS["analysis"], "tools_and_technologies": ["Terraform"]},
        }
        result = evaluate_message(conn, _message(conn, company_id, analysis=analysis), resume)
        assert _check(result, Check.COMPANY_SKILLS_REAL).passed is False

    def test_fails_for_invented_terminology(self, conn, company_id, resume):
        analysis = {**ANALYSIS, "analysis": {**ANALYSIS["analysis"], "terminology": ["synergy"]}}
        result = evaluate_message(conn, _message(conn, company_id, analysis=analysis), resume)
        assert _check(result, Check.COMPANY_SKILLS_REAL).passed is False

    def test_passes_for_terms_the_postings_use(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id), resume)
        assert _check(result, Check.COMPANY_SKILLS_REAL).passed is True


class TestCandidateSkillsAreReal:
    def test_fails_when_overlap_claims_a_skill_not_in_the_resume(self, conn, company_id, resume):
        analysis = {
            **ANALYSIS,
            "analysis": {**ANALYSIS["analysis"], "candidate_overlap": ["Python", "AWS"]},
        }
        message_id = _message(conn, company_id, analysis=analysis)
        result = evaluate_message(conn, message_id, resume)
        check = _check(result, Check.CANDIDATE_SKILLS_REAL)
        assert check.passed is False
        assert "AWS" in check.reason

    def test_passes_for_real_resume_skills(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id), resume)
        assert _check(result, Check.CANDIDATE_SKILLS_REAL).passed is True

    def test_a_smaller_resume_fails_the_same_message(self, conn, company_id):
        """The check is against the actual resume, not the stored claim."""
        thin = MasterResume(personal=Personal(full_name="Test Candidate"), skills=["Git"])
        result = evaluate_message(conn, _message(conn, company_id), thin)
        assert _check(result, Check.CANDIDATE_SKILLS_REAL).passed is False


class TestNoFabrication:
    def test_fails_on_an_invented_technology_in_the_prose(self, conn, company_id, resume):
        body = f"{GOOD_BODY} I have also worked with Kubernetes."
        result = evaluate_message(conn, _message(conn, company_id, body=body), resume)
        check = _check(result, Check.NO_FABRICATION)
        assert check.passed is False
        # The guard reports tokens lowercased.
        assert "kubernetes" in check.reason.lower()

    def test_fails_on_an_invented_number(self, conn, company_id, resume):
        body = f"{GOOD_BODY} I have 7 years of experience."
        result = evaluate_message(conn, _message(conn, company_id, body=body), resume)
        assert _check(result, Check.NO_FABRICATION).passed is False

    def test_fails_on_an_invented_claim_about_the_company(self, conn, company_id, resume):
        body = f"{GOOD_BODY} I admire your work in Fintech across Melbourne."
        result = evaluate_message(conn, _message(conn, company_id, body=body), resume)
        assert _check(result, Check.NO_FABRICATION).passed is False

    def test_fails_on_template_placeholders(self, conn, company_id, resume):
        body = f"Hi [Company], {GOOD_BODY}"
        result = evaluate_message(conn, _message(conn, company_id, body=body), resume)
        check = _check(result, Check.NO_FABRICATION)
        assert check.passed is False
        assert "placeholder" in check.reason

    def test_allows_the_companys_own_words(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id), resume)
        assert _check(result, Check.NO_FABRICATION).passed is True


class TestBannedPhrases:
    @pytest.mark.parametrize(
        "phrase",
        [
            "I am writing to express my interest.",
            "I would leverage my skills here.",
            "I bring a unique skill set.",
            "I hope this email finds you well.",
            "I am excited about the opportunity.",
        ],
    )
    def test_fails_on_cliches(self, conn, company_id, resume, phrase):
        result = evaluate_message(conn, _message(conn, company_id, body=f"{phrase} {GOOD_BODY}"), resume)
        assert _check(result, Check.NO_BANNED_PHRASES).passed is False

    @pytest.mark.parametrize(
        "phrase",
        [
            "I am applying for the Service Desk Analyst position.",
            "Please consider my application for the role.",
            "I saw your job posting for a support engineer.",
        ],
    )
    def test_fails_when_pretending_to_apply_for_an_advertised_job(
        self, conn, company_id, resume, phrase
    ):
        result = evaluate_message(conn, _message(conn, company_id, body=f"{phrase} {GOOD_BODY}"), resume)
        check = _check(result, Check.NO_BANNED_PHRASES)
        assert check.passed is False
        assert "applying" in check.reason

    def test_cliche_in_the_subject_is_caught(self, conn, company_id, resume):
        message_id = _message(conn, company_id, subject="I am writing to express interest")
        result = evaluate_message(conn, message_id, resume)
        assert _check(result, Check.NO_BANNED_PHRASES).passed is False


class TestWordCount:
    def test_fails_when_too_short(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id, body="Too short."), resume)
        check = _check(result, Check.WORD_COUNT)
        assert check.passed is False
        assert "too short" in check.reason

    def test_fails_when_too_long(self, conn, company_id, resume):
        body = " ".join(["word"] * (settings.outreach_email_max_words + 50))
        result = evaluate_message(conn, _message(conn, company_id, body=body), resume)
        assert "too long" in _check(result, Check.WORD_COUNT).reason

    def test_fails_when_body_is_empty(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id, body="   "), resume)
        assert _check(result, Check.WORD_COUNT).passed is False

    def test_fails_without_a_subject(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id, subject=""), resume)
        assert _check(result, Check.WORD_COUNT).passed is False

    def test_fails_on_an_overlong_subject(self, conn, company_id, resume):
        subject = "A subject line that simply keeps going on and on forever without stopping"
        result = evaluate_message(conn, _message(conn, company_id, subject=subject), resume)
        assert "subject is too long" in _check(result, Check.WORD_COUNT).reason


class TestDailyLimit:
    def test_passes_below_the_limit(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id), resume)
        assert _check(result, Check.DAILY_LIMIT).passed is True

    def test_fails_once_the_limit_is_reached(self, conn, company_id, resume, monkeypatch):
        import dataclasses

        monkeypatch.setattr(
            quality_gate, "settings", dataclasses.replace(settings, outreach_daily_limit=1)
        )
        sent = _message(conn, company_id)
        save_gate_result(conn, sent, passed=True, reasons=[])
        approve_message(conn, sent)
        mark_message_sent(conn, sent)
        other = insert_company(conn, {"name": "Other Co", "website": "https://other.invalid"})
        update_company_contact_email(conn, other, "careers@example.invalid")
        pending = _message(conn, other)
        conn.commit()

        result = evaluate_message(conn, pending, resume)
        check = _check(result, Check.DAILY_LIMIT)
        assert check.passed is False
        assert "daily outreach limit reached" in check.reason


class TestEnforcement:
    def test_recorded_pass_makes_a_message_approvable(self, conn, company_id, resume):
        message_id = _message(conn, company_id)
        gate = gate_and_record(conn, message_id, resume)
        conn.commit()

        assert gate.passed is True
        assert get_message(conn, message_id)["gate_passed"] == 1
        assert approve_message(conn, message_id) is True

    def test_recorded_fail_can_never_be_approved(self, conn, company_id, resume):
        """The central guarantee of this step."""
        message_id = _message(conn, company_id, body=f"{GOOD_BODY} I am certified in Kubernetes.")
        gate = gate_and_record(conn, message_id, resume)
        conn.commit()

        assert gate.passed is False
        assert get_message(conn, message_id)["gate_passed"] == 0
        assert approve_message(conn, message_id) is False
        assert get_message(conn, message_id)["status"] == OutreachStatus.DRAFT

    def test_failed_message_never_reaches_the_send_queue(self, conn, company_id, resume):
        message_id = _message(conn, company_id, body="Too short.")
        gate_and_record(conn, message_id, resume)
        conn.commit()
        assert list_approved_messages(conn) == []

    def test_reasons_are_recorded_for_review(self, conn, company_id, resume):
        message_id = _message(conn, company_id, body="Too short.")
        gate_and_record(conn, message_id, resume)
        conn.commit()

        reasons = json.loads(get_message(conn, message_id)["gate_reasons_json"])
        assert any("too short" in reason for reason in reasons)

    def test_ungated_message_is_not_eligible(self, conn, company_id):
        assert is_eligible(conn, _message(conn, company_id)) is False

    def test_passed_message_is_eligible(self, conn, company_id, resume):
        message_id = _message(conn, company_id)
        gate_and_record(conn, message_id, resume)
        conn.commit()
        assert is_eligible(conn, message_id) is True

    def test_unknown_message_is_not_eligible(self, conn):
        assert is_eligible(conn, 9999) is False

    def test_editing_invalidates_a_pass(self, conn, company_id, resume):
        message_id = _message(conn, company_id)
        gate_and_record(conn, message_id, resume)
        conn.commit()
        assert is_eligible(conn, message_id) is True

        update_message_content(conn, message_id, body="Rewritten and much too short.")
        conn.commit()
        assert is_eligible(conn, message_id) is False
        assert approve_message(conn, message_id) is False

    def test_regating_after_a_valid_edit_restores_eligibility(self, conn, company_id, resume):
        message_id = _message(conn, company_id)
        # All lowercase: a capitalised word mid-sentence would read as a
        # proper noun to the fabrication guard, which is correct behaviour.
        update_message_content(conn, message_id, body=GOOD_BODY + " and that is genuinely all of it.")
        conn.commit()

        gate = gate_and_record(conn, message_id, resume)
        conn.commit()
        assert gate.passed is True, gate.reasons
        assert approve_message(conn, message_id) is True

    def test_gate_never_sends_anything(self, conn, company_id, resume):
        message_id = _message(conn, company_id)
        gate_and_record(conn, message_id, resume)
        conn.commit()
        row = get_message(conn, message_id)
        assert row["sent_at"] is None
        assert row["send_attempts"] == 0


class TestMultipleFailures:
    def test_all_reasons_are_reported_not_just_the_first(self, conn, company_id, resume):
        message_id = _message(
            conn,
            company_id,
            body="I am writing to express my interest in Kubernetes.",
            recipient_email="someone@elsewhere.invalid",
        )
        result = evaluate_message(conn, message_id, resume)
        failed = {check.name for check in result.failures}
        assert Check.NO_BANNED_PHRASES in failed
        assert Check.VERIFIED_CONTACT_EMAIL in failed
        assert Check.WORD_COUNT in failed
        assert len(result.reasons) == len(failed)

    def test_verdict_is_fail_if_any_single_check_fails(self, conn, company_id, resume):
        result = evaluate_message(conn, _message(conn, company_id, subject=""), resume)
        assert result.verdict == FAIL
        assert len(result.failures) == 1
