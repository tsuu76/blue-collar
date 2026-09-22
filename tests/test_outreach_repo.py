"""
Tests for the outreach storage layer: company deduplication, message status
transitions, opt-out handling, and the send-queue queries.

Same conventions as test_database.py — a throwaway temp-file DB per test,
never data/jobs.db. Company names here are obviously synthetic; nothing in
this file is a real company or a real address.
"""
from __future__ import annotations

import pytest

from src.database.db import get_connection, init_db
from src.database.models import OutreachStatus, compute_company_dedupe_hash, domain_of
from src.database.outreach_repo import (
    DuplicateCompanyError,
    approve_message,
    count_sent_today,
    get_company,
    get_discovered_contacts,
    get_message,
    has_been_contacted,
    has_pending_message,
    insert_company,
    insert_message,
    is_do_not_contact,
    list_approved_messages,
    list_companies,
    list_contactable_companies,
    list_messages_by_status,
    list_messages_for_company,
    mark_message_failed,
    mark_message_sent,
    record_send_attempt,
    requeue_failed_message,
    save_discovered_contact,
    save_gate_result,
    set_do_not_contact,
    update_company_ats,
    update_company_contact_email,
    update_message_content,
    upsert_company,
)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test_outreach.db"
    init_db(path)
    return path


@pytest.fixture()
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


SAMPLE_COMPANY = {
    "name": "Example Pty Ltd",
    "website": "https://example.com",
    "contact_email": "careers@example.com",
}


def _company(conn, **overrides) -> int:
    company_id = insert_company(conn, dict(SAMPLE_COMPANY, **overrides))
    conn.commit()
    return company_id


def _message(conn, company_id, *, gate_passed=True, **overrides) -> int:
    """
    A stored message, gate-passed by default.

    approve_message now refuses anything without a recorded PASS from the
    deterministic quality gate. These tests cover the repository's own rules
    rather than the gate's, so they start from a message the gate has
    already cleared. Pass gate_passed=False (failed) or None (never gated)
    to exercise that precondition directly.
    """
    message_id = insert_message(
        conn,
        dict(
            {
                "company_id": company_id,
                "recipient_email": "careers@example.com",
                "subject": "Quick question about junior roles",
                "body": "Body text.",
            },
            **overrides,
        ),
    )
    if gate_passed is not None:
        save_gate_result(
            conn, message_id, passed=gate_passed, reasons=[] if gate_passed else ["gate failed"]
        )
    conn.commit()
    return message_id


class TestSchema:
    def test_init_db_creates_outreach_tables(self, db_path):
        connection = get_connection(db_path)
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        connection.close()
        assert {"outreach_companies", "outreach_messages"}.issubset(tables)

    def test_existing_tables_untouched(self, db_path):
        """The outreach tables are additive — the job pathway's tables must
        still all be created exactly as before."""
        connection = get_connection(db_path)
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        connection.close()
        assert {"jobs", "applications", "job_sources", "resume_versions", "cover_letters", "settings"}.issubset(tables)

    def test_init_db_is_idempotent(self, db_path):
        init_db(db_path)
        init_db(db_path)


class TestDomainOf:
    def test_extracts_domain_from_url(self):
        assert domain_of("https://example.com/careers") == "example.com"

    def test_extracts_domain_from_email(self):
        assert domain_of("careers@example.com") == "example.com"

    def test_strips_www(self):
        assert domain_of("https://www.example.com") == "example.com"

    def test_handles_bare_domain(self):
        assert domain_of("example.com") == "example.com"

    def test_strips_port(self):
        assert domain_of("http://example.com:8080/x") == "example.com"

    def test_empty_returns_empty(self):
        assert domain_of("") == ""

    def test_malformed_does_not_raise(self):
        domain_of("not a url :::")  # must not raise


class TestCompanyDedupeHash:
    def test_same_domain_same_hash_despite_name_variation(self):
        h1 = compute_company_dedupe_hash("Example Pty Ltd", "https://example.com")
        h2 = compute_company_dedupe_hash("EXAMPLE", "https://www.example.com/careers")
        assert h1 == h2

    def test_different_domain_different_hash(self):
        h1 = compute_company_dedupe_hash("Example", "https://example.com")
        h2 = compute_company_dedupe_hash("Example", "https://other.com")
        assert h1 != h2

    def test_falls_back_to_name_without_website(self):
        h1 = compute_company_dedupe_hash("Example Pty Ltd")
        h2 = compute_company_dedupe_hash("  example pty ltd  ")
        assert h1 == h2

    def test_name_fallback_differs_from_domain_hash(self):
        assert compute_company_dedupe_hash("Example") != compute_company_dedupe_hash("Example", "https://example.com")


class TestInsertCompany:
    def test_insert_and_get(self, conn):
        company_id = _company(conn)
        row = get_company(conn, company_id)
        assert row["name"] == "Example Pty Ltd"
        assert row["contact_email"] == "careers@example.com"
        assert row["do_not_contact"] == 0

    def test_blank_name_raises(self, conn):
        with pytest.raises(ValueError):
            insert_company(conn, {"name": "   ", "website": "https://example.com"})

    def test_contact_email_is_normalized(self, conn):
        company_id = _company(conn, contact_email="  Careers@Example.COM ")
        assert get_company(conn, company_id)["contact_email"] == "careers@example.com"

    def test_duplicate_raises(self, conn):
        _company(conn)
        with pytest.raises(DuplicateCompanyError):
            insert_company(conn, SAMPLE_COMPANY)

    def test_duplicate_error_carries_existing_id(self, conn):
        company_id = _company(conn)
        try:
            insert_company(conn, SAMPLE_COMPANY)
            assert False, "expected DuplicateCompanyError"
        except DuplicateCompanyError as exc:
            assert exc.existing_company_id == company_id

    def test_same_company_different_spelling_is_duplicate(self, conn):
        _company(conn)
        with pytest.raises(DuplicateCompanyError):
            insert_company(conn, {"name": "EXAMPLE", "website": "https://www.example.com/careers"})

    def test_different_company_not_duplicate(self, conn):
        id1 = _company(conn)
        id2 = insert_company(conn, {"name": "Other Co", "website": "https://other.example.org"})
        conn.commit()
        assert id1 != id2

    def test_ats_fields_default_to_null(self, conn):
        row = get_company(conn, _company(conn))
        assert row["platform"] is None
        assert row["identifier"] is None

    def test_stores_ats_fields(self, conn):
        company_id = _company(conn, platform="Greenhouse", identifier="exampleco")
        row = get_company(conn, company_id)
        assert row["platform"] == "greenhouse"  # normalized
        assert row["identifier"] == "exampleco"

    def test_update_company_ats(self, conn):
        company_id = _company(conn)
        update_company_ats(conn, company_id, "LEVER", " exampleco ")
        conn.commit()
        row = get_company(conn, company_id)
        assert row["platform"] == "lever"
        assert row["identifier"] == "exampleco"

    def test_update_company_ats_can_clear(self, conn):
        company_id = _company(conn, platform="greenhouse", identifier="exampleco")
        update_company_ats(conn, company_id, "", "")
        conn.commit()
        row = get_company(conn, company_id)
        assert row["platform"] is None
        assert row["identifier"] is None

    def test_list_companies(self, conn):
        id1 = _company(conn)
        id2 = insert_company(conn, {"name": "Other Co", "website": "https://other.example.org"})
        conn.commit()
        assert {row["id"] for row in list_companies(conn)} == {id1, id2}


class TestUpsertCompany:
    def test_creates_when_absent(self, conn):
        company_id = upsert_company(conn, SAMPLE_COMPANY)
        conn.commit()
        assert get_company(conn, company_id) is not None

    def test_returns_existing_id_instead_of_raising(self, conn):
        company_id = _company(conn)
        again = upsert_company(conn, SAMPLE_COMPANY)
        conn.commit()
        assert again == company_id

    def test_does_not_overwrite_corrected_contact_email(self, conn):
        """A hand-corrected address must survive a re-run of any future
        discovery step."""
        company_id = _company(conn)
        update_company_contact_email(conn, company_id, "jobs@example.com")
        conn.commit()
        upsert_company(conn, SAMPLE_COMPANY)
        conn.commit()
        assert get_company(conn, company_id)["contact_email"] == "jobs@example.com"

    def test_blank_name_raises(self, conn):
        with pytest.raises(ValueError):
            upsert_company(conn, {"name": ""})


class TestContactEmail:
    def test_update_normalizes(self, conn):
        company_id = _company(conn)
        update_company_contact_email(conn, company_id, "  Jobs@Example.com ")
        conn.commit()
        assert get_company(conn, company_id)["contact_email"] == "jobs@example.com"

    def test_clearing_sets_null(self, conn):
        company_id = _company(conn)
        update_company_contact_email(conn, company_id, "")
        conn.commit()
        assert get_company(conn, company_id)["contact_email"] is None

    def test_contactable_excludes_company_without_email(self, conn):
        with_email = _company(conn)
        insert_company(conn, {"name": "No Email Co", "website": "https://noemail.example.org"})
        conn.commit()
        assert [row["id"] for row in list_contactable_companies(conn)] == [with_email]

    def test_contactable_excludes_opted_out_company(self, conn):
        company_id = _company(conn)
        set_do_not_contact(conn, company_id, "asked to be removed")
        conn.commit()
        assert list_contactable_companies(conn) == []


class TestMessages:
    def test_insert_defaults_to_draft(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        assert get_message(conn, message_id)["status"] == OutreachStatus.DRAFT

    def test_recipient_email_is_normalized(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id, recipient_email=" Careers@Example.COM ")
        assert get_message(conn, message_id)["recipient_email"] == "careers@example.com"

    def test_list_by_status(self, conn):
        company_id = _company(conn)
        draft = _message(conn, company_id)
        assert [row["id"] for row in list_messages_by_status(conn, OutreachStatus.DRAFT)] == [draft]

    def test_list_by_status_respects_limit(self, conn):
        company_id = _company(conn)
        _message(conn, company_id)
        _message(conn, company_id)
        assert len(list_messages_by_status(conn, OutreachStatus.DRAFT, limit=1)) == 1

    def test_list_for_company(self, conn):
        company_id = _company(conn)
        m1 = _message(conn, company_id)
        m2 = _message(conn, company_id)
        assert [row["id"] for row in list_messages_for_company(conn, company_id)] == [m1, m2]

    def test_cascade_delete_with_company(self, conn):
        company_id = _company(conn)
        _message(conn, company_id)
        conn.execute("DELETE FROM outreach_companies WHERE id = ?", (company_id,))
        conn.commit()
        assert list_messages_for_company(conn, company_id) == []


class TestEditDraft:
    def test_draft_is_editable(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        assert update_message_content(conn, message_id, subject="New subject") is True
        conn.commit()
        assert get_message(conn, message_id)["subject"] == "New subject"

    def test_approved_message_is_not_editable(self, conn):
        """Editing after approval would make the approval meaningless."""
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        approve_message(conn, message_id)
        conn.commit()
        assert update_message_content(conn, message_id, body="Sneaky rewrite") is False
        conn.commit()
        assert get_message(conn, message_id)["body"] == "Body text."


class TestApproval:
    def test_approve_draft(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        assert approve_message(conn, message_id) is True
        conn.commit()
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.APPROVED
        assert row["approved_at"] is not None

    def test_approving_sent_message_is_noop(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        approve_message(conn, message_id)
        mark_message_sent(conn, message_id)
        conn.commit()
        assert approve_message(conn, message_id) is False
        conn.commit()
        assert get_message(conn, message_id)["status"] == OutreachStatus.SENT

    def test_ungated_message_cannot_be_approved(self, conn):
        """Absence of a gate verdict is treated as a failure, never as
        permission — a message nothing ever checked can't become sendable."""
        company_id = _company(conn)
        message_id = _message(conn, company_id, gate_passed=None)
        assert approve_message(conn, message_id) is False
        conn.commit()
        assert get_message(conn, message_id)["status"] == OutreachStatus.DRAFT

    def test_gate_failed_message_cannot_be_approved(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id, gate_passed=False)
        assert approve_message(conn, message_id) is False
        conn.commit()
        assert get_message(conn, message_id)["status"] == OutreachStatus.DRAFT

    def test_gate_failure_keeps_it_out_of_the_send_queue(self, conn):
        company_id = _company(conn)
        _message(conn, company_id, gate_passed=False)
        assert list_approved_messages(conn) == []

    def test_editing_clears_the_gate_verdict(self, conn):
        """An edit invalidates the verdict, so edited wording can't inherit
        approval the earlier text earned."""
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        assert get_message(conn, message_id)["gate_passed"] == 1

        update_message_content(conn, message_id, body="Rewritten body.")
        conn.commit()
        assert get_message(conn, message_id)["gate_passed"] is None
        assert approve_message(conn, message_id) is False

    def test_approving_opted_out_message_is_noop(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        set_do_not_contact(conn, company_id, "opted out")
        conn.commit()
        assert approve_message(conn, message_id) is False
        conn.commit()
        assert get_message(conn, message_id)["status"] == OutreachStatus.DO_NOT_CONTACT


class TestSendQueue:
    def test_unapproved_draft_cannot_be_marked_sent(self, conn):
        """The core guarantee of the review queue: only what was approved
        can be recorded as sent."""
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        assert mark_message_sent(conn, message_id) is False
        conn.commit()
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.DRAFT
        assert row["sent_at"] is None

    def test_approved_message_can_be_sent(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        approve_message(conn, message_id)
        conn.commit()
        # The sender counts the attempt before the SMTP call; mark_message_sent
        # only records the confirmed outcome, so it does not count again.
        record_send_attempt(conn, message_id)
        assert mark_message_sent(conn, message_id) is True
        conn.commit()
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.SENT
        assert row["sent_at"] is not None
        assert row["send_attempts"] == 1

    def test_failure_records_error_and_attempt(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        approve_message(conn, message_id)
        conn.commit()
        record_send_attempt(conn, message_id)
        assert mark_message_failed(conn, message_id, "SMTP connection refused") is True
        conn.commit()
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.FAILED
        assert row["last_error"] == "SMTP connection refused"
        assert row["send_attempts"] == 1
        assert row["sent_at"] is None

    def test_failed_message_can_be_retried_and_clears_error(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        approve_message(conn, message_id)
        record_send_attempt(conn, message_id)
        mark_message_failed(conn, message_id, "temporary failure")
        conn.commit()
        # A retry: requeue, count the second attempt, then succeed.
        assert requeue_failed_message(conn, message_id) is True
        record_send_attempt(conn, message_id)
        assert mark_message_sent(conn, message_id) is True
        conn.commit()
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.SENT
        assert row["last_error"] is None
        assert row["send_attempts"] == 2

    def test_queue_lists_only_approved(self, conn):
        company_id = _company(conn)
        _message(conn, company_id)
        approved = _message(conn, company_id)
        approve_message(conn, approved)
        conn.commit()
        assert [row["id"] for row in list_approved_messages(conn)] == [approved]

    def test_queue_excludes_company_opted_out_after_approval(self, conn):
        """A company can opt out between approval and sending — the queue
        must reflect the current answer, not the one at approval time."""
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        approve_message(conn, message_id)
        conn.commit()
        assert len(list_approved_messages(conn)) == 1

        set_do_not_contact(conn, company_id, "opted out")
        conn.commit()
        assert list_approved_messages(conn) == []

    def test_queue_respects_limit(self, conn):
        company_id = _company(conn)
        for _ in range(3):
            approve_message(conn, _message(conn, company_id))
        conn.commit()
        assert len(list_approved_messages(conn, limit=2)) == 2


class TestDoNotContact:
    def test_flags_company(self, conn):
        company_id = _company(conn)
        set_do_not_contact(conn, company_id, "asked not to be contacted")
        conn.commit()
        row = get_company(conn, company_id)
        assert row["do_not_contact"] == 1
        assert row["do_not_contact_reason"] == "asked not to be contacted"
        assert is_do_not_contact(conn, company_id) is True

    def test_blocks_queued_messages(self, conn):
        company_id = _company(conn)
        draft = _message(conn, company_id)
        approved = _message(conn, company_id)
        approve_message(conn, approved)
        conn.commit()

        blocked = set_do_not_contact(conn, company_id, "opted out")
        conn.commit()
        assert blocked == 2
        assert get_message(conn, draft)["status"] == OutreachStatus.DO_NOT_CONTACT
        assert get_message(conn, approved)["status"] == OutreachStatus.DO_NOT_CONTACT

    def test_leaves_sent_history_alone(self, conn):
        """Already-sent messages are history, not intent — an opt-out must
        not rewrite the record of what was actually sent."""
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        approve_message(conn, message_id)
        mark_message_sent(conn, message_id)
        conn.commit()

        set_do_not_contact(conn, company_id, "opted out")
        conn.commit()
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.SENT
        assert row["sent_at"] is not None

    def test_is_do_not_contact_false_by_default(self, conn):
        assert is_do_not_contact(conn, _company(conn)) is False

    def test_is_do_not_contact_unknown_company_is_false(self, conn):
        assert is_do_not_contact(conn, 9999) is False


class TestCountsAndGuards:
    def test_count_sent_today_starts_at_zero(self, conn):
        _company(conn)
        assert count_sent_today(conn) == 0

    def test_count_sent_today_counts_sends(self, conn):
        company_id = _company(conn)
        for _ in range(2):
            message_id = _message(conn, company_id)
            approve_message(conn, message_id)
            mark_message_sent(conn, message_id)
        conn.commit()
        assert count_sent_today(conn) == 2

    def test_count_sent_today_ignores_drafts_and_failures(self, conn):
        company_id = _company(conn)
        _message(conn, company_id)
        failed = _message(conn, company_id)
        approve_message(conn, failed)
        mark_message_failed(conn, failed, "nope")
        conn.commit()
        assert count_sent_today(conn) == 0

    def test_has_pending_message(self, conn):
        company_id = _company(conn)
        assert has_pending_message(conn, company_id) is False
        message_id = _message(conn, company_id)
        assert has_pending_message(conn, company_id) is True
        approve_message(conn, message_id)
        conn.commit()
        assert has_pending_message(conn, company_id) is True
        mark_message_sent(conn, message_id)
        conn.commit()
        assert has_pending_message(conn, company_id) is False

    def test_has_been_contacted(self, conn):
        company_id = _company(conn)
        message_id = _message(conn, company_id)
        assert has_been_contacted(conn, company_id) is False
        approve_message(conn, message_id)
        mark_message_sent(conn, message_id)
        conn.commit()
        assert has_been_contacted(conn, company_id) is True


class TestDiscoveredContactsInventory:
    """
    save_discovered_contact grew an `all_addresses` kwarg. This class
    exercises the new column in isolation — merge, dedupe, "never
    clear what's known", and the read accessor.
    """

    def test_get_returns_empty_when_nothing_stored(self, conn):
        company_id = _company(conn)
        assert get_discovered_contacts(conn, company_id) == []

    def test_persists_full_inventory(self, conn):
        company_id = _company(conn, contact_email="")
        save_discovered_contact(
            conn,
            company_id,
            email="careers@example.com",
            source="careers_page",
            evidence_url="https://example.com/careers",
            all_addresses=[
                {"email": "careers@example.com", "source": "careers_page",
                 "evidence_url": "https://example.com/careers"},
                {"email": "jobs@example.com", "source": "careers_page",
                 "evidence_url": "https://example.com/jobs"},
                {"email": "hiring@example.com", "source": "job_posting",
                 "evidence_url": "https://example.com/jobs/1"},
            ],
        )
        conn.commit()
        inventory = get_discovered_contacts(conn, company_id)
        emails = {c["email"] for c in inventory}
        assert emails == {"careers@example.com", "jobs@example.com", "hiring@example.com"}
        # Every stored address carries its evidence URL — the whole
        # point of this column is verifiability.
        for entry in inventory:
            assert entry["evidence_url"]

    def test_merge_deduplicates_on_lowercased_email(self, conn):
        company_id = _company(conn, contact_email="")
        save_discovered_contact(
            conn,
            company_id,
            all_addresses=[
                {"email": "Careers@Example.com", "source": "careers_page",
                 "evidence_url": "https://example.com/careers"},
            ],
        )
        conn.commit()
        # A second run finds the same address at a new evidence URL —
        # the earliest evidence_url must be preserved (existing wins).
        save_discovered_contact(
            conn,
            company_id,
            all_addresses=[
                {"email": "careers@example.com", "source": "job_posting",
                 "evidence_url": "https://example.com/jobs/2"},
            ],
        )
        conn.commit()
        inventory = get_discovered_contacts(conn, company_id)
        assert len(inventory) == 1
        assert inventory[0]["email"] == "careers@example.com"
        assert inventory[0]["evidence_url"] == "https://example.com/careers"

    def test_empty_all_addresses_preserves_prior_inventory(self, conn):
        company_id = _company(conn, contact_email="")
        save_discovered_contact(
            conn,
            company_id,
            all_addresses=[
                {"email": "careers@example.com", "source": "careers_page",
                 "evidence_url": "https://example.com/careers"},
            ],
        )
        conn.commit()
        # A run where the site was down contributes nothing — must
        # NOT wipe the prior inventory.
        save_discovered_contact(conn, company_id, all_addresses=[])
        conn.commit()
        assert len(get_discovered_contacts(conn, company_id)) == 1

    def test_malformed_entries_are_silently_dropped(self, conn):
        company_id = _company(conn, contact_email="")
        save_discovered_contact(
            conn,
            company_id,
            all_addresses=[
                {"email": "careers@example.com", "source": "careers_page",
                 "evidence_url": "https://example.com/careers"},
                "not-a-dict",
                {"email": "", "source": "careers_page"},
                {"no_email_key": True},
            ],
        )
        conn.commit()
        inventory = get_discovered_contacts(conn, company_id)
        assert {c["email"] for c in inventory} == {"careers@example.com"}
