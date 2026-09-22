"""
Tests for the /outreach UI: rendering, outcome reasons, the discovered-contact
display, and the approve/reject/edit/bulk routes that remain as a manual
fallback behind the automatic pathway.

Follows tests/test_dashboard.py's fixtures — temp DB via init_db, Flask test
client from create_app. All companies and drafts here are synthetic.
"""
from __future__ import annotations

import json

import pytest

from src.dashboard.app import company_state, create_app
from src.database.db import get_connection, init_db
from src.database.models import OutreachStatus
from src.database.outreach_repo import (
    approve_message,
    get_message,
    insert_company,
    insert_message,
    mark_message_sent,
    save_company_research,
    save_gate_result,
    set_do_not_contact,
)
from src.resume.schema import Bullet, Education, MasterResume, Personal, Project


@pytest.fixture(autouse=True)
def synthetic_resume(monkeypatch):
    """
    The approve route re-runs the real quality gate, which loads the master
    resume from disk. Point it at a synthetic one so these tests never
    depend on the developer's actual resume file.
    """
    resume = MasterResume(
        personal=Personal(full_name="Test Candidate", email="test@example.invalid"),
        summary="First-year IT student.",
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
    monkeypatch.setattr("src.resume.store.load_master_resume", lambda *a, **k: resume)
    return resume


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test_outreach_dash.db"
    init_db(path)
    return path


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


RESEARCH = {
    "company": "Example Co",
    "platform": "greenhouse",
    "identifier": "exampleco",
    "posting_count": 2,
    "titles": ["Service Desk Analyst", "Junior Support Engineer"],
    "skills": ["Python", "SQL"],
    "postings": [
        {"title": "Service Desk Analyst", "url": "https://example.invalid/1", "location": "Sydney"},
    ],
    "error": None,
}

ANALYSIS = {
    "analysis": {
        "recurring_skills": ["Python", "SQL"],
        "tools_and_technologies": ["Python"],
        "responsibilities": ["Troubleshoot customer issues"],
        "experience_requirements": ["Entry level"],
        "terminology": ["Service Desk"],
        "candidate_overlap": ["Python"],
        "notes": "They hire support-focused technical staff.",
    },
    "dropped_company_terms": ["Kubernetes"],
    "dropped_candidate_claims": ["AWS"],
}


def _company(conn, *, name="Example Co", website="https://example.invalid", email="careers@example.invalid", researched=True, **kw):
    company_id = insert_company(
        conn,
        {"name": name, "website": website, "contact_email": email, "platform": "greenhouse", "identifier": "exampleco", **kw},
    )
    if researched:
        save_company_research(conn, company_id, RESEARCH)
    conn.commit()
    return company_id


SNAPSHOT = {
    "company": "Example Co",
    "platform": "greenhouse",
    "identifier": "exampleco",
    "postings": [
        {
            "title": "Service Desk Analyst",
            "url": "https://example.invalid/1",
            "location": "Sydney",
            "description": (
                "Troubleshoot customer issues on our service desk. Python and SQL are used "
                "daily by the team. Entry level applicants welcome."
            ),
        }
    ],
}

# A body that genuinely passes the gate against the synthetic resume above
# and the postings in SNAPSHOT — right length, no clichés, only real skills.
GATE_SAFE_BODY = " ".join(
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


def _draft(conn, company_id, *, body=GATE_SAFE_BODY, **kw):
    message_id = insert_message(
        conn,
        {
            "company_id": company_id,
            "recipient_email": "careers@example.invalid",
            "subject": "Student interested in entry-level work",
            "body": body,
            "analysis": ANALYSIS,
            "research_snapshot": SNAPSHOT,
            **kw,
        },
    )
    # The pipeline records a gate verdict when it drafts; these fixtures
    # stand in for that. The approve route re-runs the gate for real
    # regardless, so this only sets the starting state.
    save_gate_result(conn, message_id, passed=True, reasons=[])
    conn.commit()
    return message_id


class TestExistingDashboardUntouched:
    def test_job_board_still_renders(self, client):
        """
        The outreach feature must not disturb the pipeline board. The
        board's URL moved from `/` to `/board` when the client-facing
        landing page took over `/` — the assertion is on the board
        rendering intact, which is exactly what this checks at its new
        URL.
        """
        response = client.get("/board")
        assert response.status_code == 200
        assert b"READY TO APPLY" in response.data

    def test_outreach_link_in_nav(self, client):
        # Outreach is now in the top-bar's secondary actions group,
        # still one click away from the landing page.
        assert b'href="/outreach"' in client.get("/").data


class TestOutreachPage:
    def test_renders_when_empty(self, client):
        response = client.get("/outreach")
        assert response.status_code == 200
        assert b"No outreach companies yet" in response.data

    def test_empty_state_invents_nothing(self, client):
        """An empty database must show an empty page, never demo data."""
        body = client.get("/outreach").data
        assert b"outreach_companies.json" in body
        assert b"Example Co" not in body

    def test_lists_companies_with_research(self, client, conn):
        _company(conn)
        response = client.get("/outreach")
        assert response.status_code == 200
        assert b"Example Co" in response.data
        assert b"greenhouse" in response.data
        assert b"Service Desk Analyst" in response.data
        assert b"Python" in response.data

    def test_shows_draft(self, client, conn):
        company_id = _company(conn)
        _draft(conn, company_id)
        response = client.get("/outreach")
        assert b"Student interested in entry-level work" in response.data

    def test_no_approval_controls(self, client, conn):
        """
        Sending is automatic, so the page must not ask for an approval it
        does not need. The routes still exist as a fallback; the buttons do
        not.
        """
        company_id = _company(conn)
        _draft(conn, company_id)
        body = client.get("/outreach").data
        assert b"Approve selected" not in body
        assert b"Approve draft" not in body
        assert b"approve-selected" not in body

    def test_draft_page_offers_no_approve_button(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        body = client.get(f"/outreach/draft/{message_id}").data
        assert b"Approve draft" not in body
        assert b"sends automatically" in body
        # Stopping a message is still possible.
        assert b"Reject draft" in body


class TestContactIsShown:
    def test_a_discovered_address_shows_where_it_came_from(self, client, conn):
        from src.database.outreach_repo import save_discovered_contact

        company_id = _company(conn, email="")
        save_discovered_contact(
            conn,
            company_id,
            email="careers@example.invalid",
            source="careers_page",
            evidence_url="https://example.invalid/careers",
        )
        conn.commit()
        body = client.get("/outreach").data
        assert b"careers@example.invalid" in body
        assert b"published on their site" in body
        assert b"https://example.invalid/careers" in body

    def test_linkedin_is_shown_for_manual_use(self, client, conn):
        from src.database.outreach_repo import save_discovered_contact

        company_id = _company(conn, email="")
        save_discovered_contact(
            conn,
            company_id,
            name="Dana Example",
            linkedin_url="https://www.linkedin.com/in/dana-example",
        )
        conn.commit()
        body = client.get("/outreach").data
        assert b"https://www.linkedin.com/in/dana-example" in body
        assert b"Dana Example" in body
        assert b"contact by hand" in body

    def test_a_company_with_no_contact_says_none_found(self, client, conn):
        _company(conn, email="")
        body = client.get("/outreach").data
        assert b"None found" in body


class TestEligibilityReasons:
    def _state(self, conn, company_id):
        from src.database.outreach_repo import get_company, list_messages_for_company

        company = get_company(conn, company_id)
        research = json.loads(company["research_json"]) if company["research_json"] else {}
        return company_state(company, research, list_messages_for_company(conn, company_id))

    def test_do_not_contact_reason(self, client, conn):
        company_id = _company(conn)
        set_do_not_contact(conn, company_id, "asked not to be contacted")
        conn.commit()
        state = self._state(conn, company_id)
        assert state["state"] == "DO_NOT_CONTACT"
        assert "asked not to be contacted" in state["reason"]
        assert b"asked not to be contacted" in client.get("/outreach").data

    def test_missing_contact_email_reason(self, client, conn):
        company_id = _company(conn, email="")
        state = self._state(conn, company_id)
        assert state["state"] == "NEEDS_CONTACT"
        assert "no verified contact email" in state["reason"]
        assert b"no verified contact email" in client.get("/outreach").data

    def test_no_postings_reason(self, conn):
        company_id = _company(conn, researched=False)
        save_company_research(conn, company_id, {"posting_count": 0, "error": "no supported ATS board configured"})
        conn.commit()
        state = self._state(conn, company_id)
        assert state["state"] == "NO_POSTINGS"
        assert "no supported ATS board configured" in state["reason"]

    def test_not_researched_reason(self, conn):
        company_id = _company(conn, researched=False)
        state = self._state(conn, company_id)
        assert state["state"] == "NOT_RESEARCHED"
        assert "hasn't been researched" in state["reason"]

    def test_eligible_company_has_no_reason(self, conn):
        state = self._state(conn, _company(conn))
        assert state["state"] == "READY_TO_DRAFT"
        assert state["reason"] == ""

    def test_do_not_contact_overrides_everything(self, conn):
        company_id = _company(conn)
        _draft(conn, company_id)
        set_do_not_contact(conn, company_id, "opted out")
        conn.commit()
        assert self._state(conn, company_id)["state"] == "DO_NOT_CONTACT"

    def test_state_reflects_existing_draft(self, conn):
        company_id = _company(conn)
        _draft(conn, company_id)
        assert self._state(conn, company_id)["state"] == "DRAFT_READY"

    def test_state_reflects_sent(self, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        approve_message(conn, message_id)
        mark_message_sent(conn, message_id)
        conn.commit()
        state = self._state(conn, company_id)
        assert state["state"] == "SENT"
        assert state["label"] == "Sent"
        assert "already contacted" in state["reason"]

    def test_state_reflects_a_failed_send(self, conn):
        from src.database.outreach_repo import mark_message_failed

        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        approve_message(conn, message_id)
        mark_message_failed(conn, message_id, "SMTPRecipientsRefused: mailbox unavailable")
        conn.commit()
        state = self._state(conn, company_id)
        assert state["state"] == "FAILED"
        assert state["label"] == "Failed to send"
        assert "mailbox unavailable" in state["reason"]

    def test_state_reflects_a_gate_failure(self, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        save_gate_result(conn, message_id, passed=False, reasons=["claims an unsupported skill"])
        conn.commit()
        state = self._state(conn, company_id)
        assert state["state"] == "GATE_FAILED"
        assert state["label"] == "Gate failed"
        assert "unsupported skill" in state["reason"]

    def test_a_sent_message_outranks_a_later_failure(self, conn):
        """A company that has been emailed reads as contacted, whatever else
        is in its history."""
        from src.database.outreach_repo import mark_message_failed

        company_id = _company(conn)
        sent_id = _draft(conn, company_id)
        approve_message(conn, sent_id)
        mark_message_sent(conn, sent_id)
        other_id = _draft(conn, company_id)
        approve_message(conn, other_id)
        mark_message_failed(conn, other_id, "boom")
        conn.commit()
        assert self._state(conn, company_id)["state"] == "SENT"


class TestDraftPage:
    def test_renders_draft_with_reasoning(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        response = client.get(f"/outreach/draft/{message_id}")
        assert response.status_code == 200
        assert b"expense tracker" in response.data
        assert b"careers@example.invalid" in response.data
        assert b"Service Desk" in response.data
        assert b"They hire support-focused technical staff." in response.data

    def test_shows_what_verification_discarded(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        body = client.get(f"/outreach/draft/{message_id}").data
        assert b"Discarded by verification" in body
        assert b"AWS" in body
        assert b"Kubernetes" in body

    def test_unknown_draft_404s(self, client):
        assert client.get("/outreach/draft/9999").status_code == 404

    def test_draft_is_editable(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        assert b"<textarea" in client.get(f"/outreach/draft/{message_id}").data

    def test_approved_message_is_read_only(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        approve_message(conn, message_id)
        conn.commit()
        body = client.get(f"/outreach/draft/{message_id}").data
        assert b"<textarea" not in body
        assert b"can no longer be edited" in body


class TestActions:
    def test_approve_changes_status_only(self, client, conn):
        """Approval must never send: status changes, sent_at stays null."""
        company_id = _company(conn)
        message_id = _draft(conn, company_id)

        response = client.post(f"/outreach/draft/{message_id}/approve", follow_redirects=True)
        assert response.status_code == 200

        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.APPROVED
        assert row["sent_at"] is None
        assert row["send_attempts"] == 0

    def test_approve_flash_says_nothing_sent(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        response = client.post(f"/outreach/draft/{message_id}/approve", follow_redirects=True)
        assert b"Nothing has been sent" in response.data

    def test_approve_unknown_message_404s(self, client):
        assert client.post("/outreach/draft/9999/approve").status_code == 404

    def test_approving_twice_is_reported_not_silent(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        client.post(f"/outreach/draft/{message_id}/approve")
        response = client.post(f"/outreach/draft/{message_id}/approve", follow_redirects=True)
        assert b"could not be approved" in response.data

    def test_reject(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        response = client.post(f"/outreach/draft/{message_id}/reject", follow_redirects=True)
        assert response.status_code == 200
        assert get_message(conn, message_id)["status"] == OutreachStatus.REJECTED

    def test_reject_unknown_message_404s(self, client):
        assert client.post("/outreach/draft/9999/reject").status_code == 404

    def test_edit_draft(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        client.post(
            f"/outreach/draft/{message_id}/edit",
            data={"subject": "Edited subject", "body": "Edited body text."},
            follow_redirects=True,
        )
        row = get_message(conn, message_id)
        assert row["subject"] == "Edited subject"
        assert row["body"] == "Edited body text."

    def test_edit_after_approval_is_refused(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        approve_message(conn, message_id)
        conn.commit()

        response = client.post(
            f"/outreach/draft/{message_id}/edit",
            data={"subject": "Sneaky", "body": "Sneaky rewrite."},
            follow_redirects=True,
        )
        assert b"can no longer be edited" in response.data
        assert get_message(conn, message_id)["subject"] == "Student interested in entry-level work"


class TestBulkApprove:
    def test_approves_selected_only(self, client, conn):
        # One draft per company: the quality gate refuses a second pending
        # message to the same company, which is the behaviour we want.
        first = _draft(conn, _company(conn))
        second = _draft(conn, _company(conn, name="Second Co", website="https://second.invalid"))
        third = _draft(conn, _company(conn, name="Third Co", website="https://third.invalid"))

        response = client.post(
            "/outreach/approve-selected",
            data={"message_ids": [str(first), str(third)]},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert get_message(conn, first)["status"] == OutreachStatus.APPROVED
        assert get_message(conn, second)["status"] == OutreachStatus.DRAFT
        assert get_message(conn, third)["status"] == OutreachStatus.APPROVED

    def test_bulk_approve_sends_nothing(self, client, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        response = client.post(
            "/outreach/approve-selected", data={"message_ids": [str(message_id)]}, follow_redirects=True
        )
        assert b"Nothing has been sent" in response.data
        assert get_message(conn, message_id)["sent_at"] is None

    def test_nothing_selected_is_reported(self, client, conn):
        _company(conn)
        response = client.post("/outreach/approve-selected", data={}, follow_redirects=True)
        assert b"No drafts selected" in response.data

    def test_ineligible_ids_are_skipped_not_fatal(self, client, conn):
        """A stale or already-sent id must not fail the whole batch."""
        good = _draft(conn, _company(conn))
        other_id = _company(conn, name="Other Co", website="https://other.invalid")
        already_sent = _draft(conn, other_id)
        approve_message(conn, already_sent)
        mark_message_sent(conn, already_sent)
        conn.commit()

        response = client.post(
            "/outreach/approve-selected",
            data={"message_ids": [str(good), str(already_sent), "9999"]},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Approved 1 draft" in response.data
        assert b"2 skipped" in response.data
        assert get_message(conn, good)["status"] == OutreachStatus.APPROVED
        assert get_message(conn, already_sent)["status"] == OutreachStatus.SENT

    def test_non_numeric_ids_are_ignored(self, client, conn):
        company_id = _company(conn)
        good = _draft(conn, company_id)
        response = client.post(
            "/outreach/approve-selected",
            data={"message_ids": [str(good), "not-a-number"]},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert get_message(conn, good)["status"] == OutreachStatus.APPROVED
