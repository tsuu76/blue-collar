"""
Tests for the SMTP sending layer.

**No real email is ever sent by these tests.** Every test uses a fake
transport that records messages in memory or raises on demand; smtplib is
never invoked, no socket is opened, and no SMTP credentials are read. One
test asserts that directly by patching smtplib to explode if touched.

All companies, addresses and bodies below are synthetic fixtures. Every
address uses the reserved .invalid TLD, which by RFC 2606 can never resolve.
"""
from __future__ import annotations

import dataclasses
import json
import smtplib

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
    requeue_failed_message,
    save_gate_result,
    set_do_not_contact,
    update_company_contact_email,
)
from src.outreach import sender as sender_module
from src.outreach.sender import (
    DryRunTransport,
    EmailTransport,
    SendBlocked,
    build_email,
    send_approved,
    send_one,
    smtp_configuration_problems,
    verify_sendable,
)


class FakeTransport(EmailTransport):
    """
    Stands in for a mail server. Records what it was asked to deliver, or
    raises to simulate a failure. Opens nothing.
    """

    def __init__(self, error: Exception | None = None, fail_after: int | None = None):
        self.messages = []
        self._error = error
        self._fail_after = fail_after

    def send(self, message):
        if self._error and (self._fail_after is None or len(self.messages) >= self._fail_after):
            raise self._error
        self.messages.append(message)

    @property
    def recipients(self):
        return [m["To"] for m in self.messages]


@pytest.fixture(autouse=True)
def smtp_is_never_touched(monkeypatch):
    """
    Hard guarantee for this whole module: if any test reaches real smtplib,
    it fails loudly instead of contacting a mail server.
    """
    def explode(*args, **kwargs):
        raise AssertionError("A test attempted to open a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", explode)
    monkeypatch.setattr(smtplib, "SMTP_SSL", explode)


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "sender.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def _company(conn, name="Example Co", website="https://example.invalid", email="careers@example.invalid"):
    company_id = insert_company(
        conn, {"name": name, "website": website, "contact_email": email}
    )
    conn.commit()
    return company_id


def _approved(conn, company_id, *, recipient="careers@example.invalid", **overrides):
    """A message that has passed the gate and been approved — i.e. sendable."""
    message_id = insert_message(
        conn,
        {
            "company_id": company_id,
            "recipient_email": recipient,
            "subject": "Student interested in entry-level work",
            "body": "Hi there,\n\nA short, honest note.\n\nThanks,\nTest",
            **overrides,
        },
    )
    save_gate_result(conn, message_id, passed=True, reasons=[])
    approve_message(conn, message_id)
    conn.commit()
    return message_id


def _draft(conn, company_id):
    message_id = insert_message(
        conn, {"company_id": company_id, "recipient_email": "careers@example.invalid",
               "subject": "s", "body": "b"}
    )
    conn.commit()
    return message_id


class TestSendQueueConditions:
    """The query must independently require all four conditions."""

    def test_approved_gated_unsent_message_is_queued(self, conn):
        message_id = _approved(conn, _company(conn))
        assert [r["id"] for r in list_approved_messages(conn)] == [message_id]

    def test_draft_is_not_queued(self, conn):
        _draft(conn, _company(conn))
        assert list_approved_messages(conn) == []

    def test_ungated_message_is_not_queued(self, conn):
        company_id = _company(conn)
        message_id = insert_message(
            conn, {"company_id": company_id, "recipient_email": "careers@example.invalid",
                   "subject": "s", "body": "b", "status": OutreachStatus.APPROVED}
        )
        conn.commit()
        assert get_message(conn, message_id)["gate_passed"] is None
        assert list_approved_messages(conn) == []

    def test_do_not_contact_company_is_not_queued(self, conn):
        company_id = _company(conn)
        _approved(conn, company_id)
        set_do_not_contact(conn, company_id, "opted out")
        conn.commit()
        assert list_approved_messages(conn) == []

    def test_already_sent_message_is_not_queued(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        mark_message_sent(conn, message_id)
        conn.commit()
        assert list_approved_messages(conn) == []


class TestVerifySendable:
    def test_passes_for_a_sendable_message(self, conn):
        company_id = _company(conn)
        message, company = verify_sendable(conn, _approved(conn, company_id))
        assert company["name"] == "Example Co"
        assert message["status"] == OutreachStatus.APPROVED

    def test_blocks_a_draft(self, conn):
        with pytest.raises(SendBlocked, match="not APPROVED"):
            verify_sendable(conn, _draft(conn, _company(conn)))

    def test_blocks_when_gate_did_not_pass(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        save_gate_result(conn, message_id, passed=False, reasons=["nope"])
        conn.commit()
        with pytest.raises(SendBlocked, match="quality gate"):
            verify_sendable(conn, message_id)

    def test_blocks_a_do_not_contact_company(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        set_do_not_contact(conn, company_id, "asked not to be contacted")
        conn.commit()
        with pytest.raises(SendBlocked, match="asked not to be contacted"):
            verify_sendable(conn, message_id)

    def test_blocks_an_already_sent_message(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        mark_message_sent(conn, message_id)
        conn.commit()
        with pytest.raises(SendBlocked, match="already been sent"):
            verify_sendable(conn, message_id)

    def test_blocks_a_redirected_recipient(self, conn):
        """The recipient re-check: an address that is no longer the
        company's verified one must not be emailed."""
        company_id = _company(conn)
        message_id = _approved(conn, company_id, recipient="someone@elsewhere.invalid")
        with pytest.raises(SendBlocked, match="elsewhere.invalid"):
            verify_sendable(conn, message_id)

    def test_blocks_when_the_company_address_was_cleared(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        update_company_contact_email(conn, company_id, "")
        conn.commit()
        with pytest.raises(SendBlocked, match="verified contact address"):
            verify_sendable(conn, message_id)

    def test_case_difference_is_not_a_redirect(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id, recipient="Careers@Example.invalid")
        verify_sendable(conn, message_id)  # must not raise

    def test_blocks_a_missing_message(self, conn):
        with pytest.raises(SendBlocked, match="no longer exists"):
            verify_sendable(conn, 9999)


class TestSendOne:
    def test_sends_and_records(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        transport = FakeTransport()

        result = send_one(conn, message_id, transport, dry_run=False)

        assert result.sent is True
        assert transport.recipients == ["careers@example.invalid"]
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.SENT
        assert row["sent_at"] is not None
        assert row["send_attempts"] == 1
        assert row["last_error"] is None

    def test_blocked_message_is_never_handed_to_the_transport(self, conn):
        transport = FakeTransport()
        result = send_one(conn, _draft(conn, _company(conn)), transport, dry_run=False)
        assert result.sent is False
        assert transport.messages == []

    def test_never_marks_sent_before_the_transport_returns(self, conn):
        """The transport inspects the database mid-send: the message must
        still be unsent at that moment."""
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        observed = {}

        class InspectingTransport(EmailTransport):
            def send(self, message):
                row = get_message(conn, message_id)
                observed["status"] = row["status"]
                observed["sent_at"] = row["sent_at"]
                observed["attempts"] = row["send_attempts"]

        send_one(conn, message_id, InspectingTransport(), dry_run=False)

        assert observed["status"] == OutreachStatus.APPROVED
        assert observed["sent_at"] is None
        # The attempt was already counted and committed before the send.
        assert observed["attempts"] == 1

    def test_failure_records_the_error_and_keeps_the_message(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        transport = FakeTransport(error=smtplib.SMTPAuthenticationError(535, b"bad credentials"))

        result = send_one(conn, message_id, transport, dry_run=False)

        assert result.sent is False
        assert "SMTPAuthenticationError" in result.error
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.FAILED
        assert row["sent_at"] is None
        assert row["send_attempts"] == 1
        assert "bad credentials" in row["last_error"]
        # Nothing was lost.
        assert row["subject"] == "Student interested in entry-level work"
        assert row["body"].startswith("Hi there,")

    def test_failure_does_not_raise(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        result = send_one(conn, message_id, FakeTransport(error=OSError("network down")), dry_run=False)
        assert result.error

    def test_failed_message_leaves_the_send_queue(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        send_one(conn, message_id, FakeTransport(error=OSError("nope")), dry_run=False)
        assert list_approved_messages(conn) == []

    def test_failed_message_can_be_requeued_after_a_fix(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        send_one(conn, message_id, FakeTransport(error=OSError("nope")), dry_run=False)
        conn.commit()

        assert requeue_failed_message(conn, message_id) is True
        conn.commit()
        assert [r["id"] for r in list_approved_messages(conn)] == [message_id]

        transport = FakeTransport()
        assert send_one(conn, message_id, transport, dry_run=False).sent is True
        assert get_message(conn, message_id)["send_attempts"] == 2


class TestDuplicateProtection:
    def test_sending_twice_transmits_once(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        transport = FakeTransport()

        first = send_one(conn, message_id, transport, dry_run=False)
        second = send_one(conn, message_id, transport, dry_run=False)

        assert first.sent is True
        assert second.sent is False
        assert "already been sent" in second.skipped_reason
        assert len(transport.messages) == 1

    def test_running_the_whole_drain_twice_transmits_once(self, conn):
        _approved(conn, _company(conn))
        transport = FakeTransport()

        send_approved(conn=conn, transport=transport, dry_run=False)
        send_approved(conn=conn, transport=transport, dry_run=False)

        assert len(transport.messages) == 1

    def test_mark_sent_is_refused_for_an_already_sent_message(self, conn):
        """The database-level backstop against a double record."""
        company_id = _company(conn)
        message_id = _approved(conn, company_id)
        assert mark_message_sent(conn, message_id) is True
        assert mark_message_sent(conn, message_id) is False

    def test_an_interrupted_send_leaves_evidence(self, conn):
        """If the process dies after SMTP but before the commit, the attempt
        count shows an attempt was in flight — it is not a silent gap."""
        company_id = _company(conn)
        message_id = _approved(conn, company_id)

        class CrashingTransport(EmailTransport):
            def send(self, message):
                raise KeyboardInterrupt("process killed mid-delivery")

        with pytest.raises(KeyboardInterrupt):
            send_one(conn, message_id, CrashingTransport(), dry_run=False)

        row = get_message(conn, message_id)
        assert row["send_attempts"] == 1
        assert row["sent_at"] is None


class TestDryRun:
    def test_dry_run_is_the_default(self, conn):
        _approved(conn, _company(conn))
        transport = FakeTransport()
        run = send_approved(conn=conn, transport=transport)
        assert run.dry_run is settings.outreach_dry_run

    def test_dry_run_changes_nothing_in_the_database(self, conn):
        company_id = _company(conn)
        message_id = _approved(conn, company_id)

        run = send_approved(conn=conn, transport=FakeTransport(), dry_run=True)

        assert run.sent == 0
        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.APPROVED
        assert row["sent_at"] is None
        assert row["send_attempts"] == 0

    def test_dry_run_still_runs_every_check(self, conn):
        company_id = _company(conn)
        _approved(conn, company_id, recipient="someone@elsewhere.invalid")
        run = send_approved(conn=conn, transport=FakeTransport(), dry_run=True)
        assert "elsewhere.invalid" in run.results[0].skipped_reason

    def test_dry_run_transport_opens_nothing(self, conn):
        company_id = _company(conn)
        _approved(conn, company_id)
        transport = DryRunTransport()
        send_approved(conn=conn, transport=transport, dry_run=True)
        assert len(transport.messages) == 1  # built, not transmitted

    def test_dry_run_needs_no_smtp_configuration(self, conn, monkeypatch):
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, smtp_host="", smtp_from_email="")
        )
        _approved(conn, _company(conn))
        run = send_approved(conn=conn, dry_run=True)  # must not raise
        assert run.sent == 0


class TestDailyLimit:
    def _many(self, conn, n):
        ids = []
        for i in range(n):
            company_id = _company(
                conn, name=f"Co {i}", website=f"https://co{i}.invalid", email=f"careers@co{i}.invalid"
            )
            ids.append(_approved(conn, company_id, recipient=f"careers@co{i}.invalid"))
        return ids

    def test_stops_at_the_configured_limit(self, conn, monkeypatch):
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_daily_limit=2)
        )
        self._many(conn, 5)
        transport = FakeTransport()

        run = send_approved(conn=conn, transport=transport, dry_run=False)

        assert run.sent == 2
        assert len(transport.messages) == 2

    def test_counts_messages_already_sent_today(self, conn, monkeypatch):
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_daily_limit=3)
        )
        ids = self._many(conn, 5)
        mark_message_sent(conn, ids[0])
        conn.commit()

        run = send_approved(conn=conn, transport=FakeTransport(), dry_run=False)

        assert run.already_sent_today == 1
        assert run.sent == 2  # 3 minus the one already sent

    def test_explicit_limit_narrows_further(self, conn):
        self._many(conn, 4)
        transport = FakeTransport()
        run = send_approved(conn=conn, transport=transport, dry_run=False, limit=1)
        assert run.sent == 1
        assert len(transport.messages) == 1

    def test_explicit_limit_cannot_exceed_the_daily_cap(self, conn, monkeypatch):
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_daily_limit=1)
        )
        self._many(conn, 5)
        run = send_approved(conn=conn, transport=FakeTransport(), dry_run=False, limit=99)
        assert run.sent == 1

    def test_limit_already_reached_sends_nothing(self, conn, monkeypatch):
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_daily_limit=1)
        )
        ids = self._many(conn, 2)
        mark_message_sent(conn, ids[0])
        conn.commit()

        transport = FakeTransport()
        run = send_approved(conn=conn, transport=transport, dry_run=False)
        assert run.sent == 0
        assert transport.messages == []


class TestDrainBehaviour:
    def test_one_failure_does_not_stop_the_rest(self, conn):
        company_a = _company(conn, name="A Co", website="https://a.invalid", email="careers@a.invalid")
        company_b = _company(conn, name="B Co", website="https://b.invalid", email="careers@b.invalid")
        _approved(conn, company_a, recipient="careers@a.invalid")
        _approved(conn, company_b, recipient="careers@b.invalid")

        run = send_approved(
            conn=conn, transport=FakeTransport(error=OSError("boom"), fail_after=1), dry_run=False
        )
        assert run.sent == 1
        assert run.failed == 1

    def test_empty_queue_is_a_no_op(self, conn):
        run = send_approved(conn=conn, transport=FakeTransport(), dry_run=False)
        assert run.sent == 0
        assert run.results == []

    def test_never_approves_a_draft(self, conn):
        company_id = _company(conn)
        message_id = _draft(conn, company_id)
        send_approved(conn=conn, transport=FakeTransport(), dry_run=False)
        assert get_message(conn, message_id)["status"] == OutreachStatus.DRAFT

    def test_result_is_serializable(self, conn):
        _approved(conn, _company(conn))
        run = send_approved(conn=conn, transport=FakeTransport(), dry_run=False)
        payload = json.loads(json.dumps(run.to_dict()))
        assert payload["sent"] == 1


class TestConfiguration:
    def test_refuses_a_live_run_without_smtp_settings(self, conn, monkeypatch):
        """Better to refuse than to mark the whole queue FAILED against a
        misconfigured mailbox."""
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, smtp_host="", smtp_from_email="")
        )
        _approved(conn, _company(conn))
        with pytest.raises(ValueError, match="Cannot send"):
            send_approved(conn=conn, dry_run=False)

    def test_reports_missing_settings(self, monkeypatch):
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, smtp_host="", smtp_from_email="")
        )
        problems = smtp_configuration_problems()
        assert any("SMTP_HOST" in p for p in problems)
        assert any("SMTP_FROM_EMAIL" in p for p in problems)

    def test_flags_a_username_without_a_password(self, monkeypatch):
        monkeypatch.setattr(
            sender_module,
            "settings",
            dataclasses.replace(
                settings, smtp_host="smtp.example.invalid", smtp_from_email="me@example.invalid",
                smtp_username="me", smtp_password="",
            ),
        )
        assert any("SMTP_PASSWORD" in p for p in smtp_configuration_problems())


class TestBuildEmail:
    def test_builds_a_plain_text_email(self, conn, monkeypatch):
        monkeypatch.setattr(
            sender_module,
            "settings",
            dataclasses.replace(settings, smtp_from_email="me@example.invalid", smtp_from_name="Test Candidate"),
        )
        company_id = _company(conn)
        message = get_message(conn, _approved(conn, company_id))
        email = build_email(message)

        assert email["To"] == "careers@example.invalid"
        assert email["From"] == "Test Candidate <me@example.invalid>"
        assert email["Subject"] == "Student interested in entry-level work"
        assert email.get_content_type() == "text/plain"
        assert "A short, honest note." in email.get_content()

    def test_bare_address_when_no_display_name(self, conn, monkeypatch):
        monkeypatch.setattr(
            sender_module,
            "settings",
            dataclasses.replace(settings, smtp_from_email="me@example.invalid", smtp_from_name=""),
        )
        message = get_message(conn, _approved(conn, _company(conn)))
        assert build_email(message)["From"] == "me@example.invalid"

    def test_no_html_and_no_attachments(self, conn):
        message = get_message(conn, _approved(conn, _company(conn)))
        email = build_email(message)
        assert email.is_multipart() is False


class TestDryRunIsAnAbsoluteVeto:
    """
    OUTREACH_DRY_RUN=true must not be overridable from the command line.
    Sending is irreversible, so enabling it has to be a deliberate change in
    the environment rather than a flag on one command.
    """

    def _seeded_db(self, tmp_path):
        db_path = tmp_path / "veto.db"
        init_db(db_path)
        connection = get_connection(db_path)
        message_id = _approved(connection, _company(connection))
        connection.close()
        return db_path, message_id

    def test_live_is_refused_while_dry_run_is_on(self, tmp_path, monkeypatch, capsys):
        db_path, message_id = self._seeded_db(tmp_path)
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_dry_run=True)
        )

        exit_code = sender_module.main(["--live", "--db", str(db_path)])

        assert exit_code == 1
        output = capsys.readouterr().out
        assert "Refusing to send" in output
        assert "OUTREACH_DRY_RUN" in output

    def test_refusal_explains_how_to_change_the_switch(self, tmp_path, monkeypatch, capsys):
        db_path, _ = self._seeded_db(tmp_path)
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_dry_run=True)
        )

        sender_module.main(["--live", "--db", str(db_path)])

        output = capsys.readouterr().out
        assert "OUTREACH_DRY_RUN=false" in output
        assert ".env" in output

    def test_nothing_is_sent_when_the_veto_fires(self, tmp_path, monkeypatch, capsys):
        db_path, message_id = self._seeded_db(tmp_path)
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_dry_run=True)
        )

        sender_module.main(["--live", "--db", str(db_path)])

        connection = get_connection(db_path)
        try:
            row = get_message(connection, message_id)
        finally:
            connection.close()
        assert row["status"] == OutreachStatus.APPROVED
        assert row["sent_at"] is None
        assert row["send_attempts"] == 0

    def test_the_veto_does_not_fire_when_dry_run_is_off(self, tmp_path, monkeypatch, capsys):
        """With the switch turned off, --live proceeds to the normal
        configuration check rather than being vetoed."""
        db_path, _ = self._seeded_db(tmp_path)
        monkeypatch.setattr(
            sender_module,
            "settings",
            dataclasses.replace(settings, outreach_dry_run=False, smtp_host="", smtp_from_email=""),
        )

        sender_module.main(["--live", "--db", str(db_path)])

        output = capsys.readouterr().out
        assert "Refusing to send" not in output
        assert "Cannot send" in output  # the SMTP-configuration check, not the veto

    def test_a_plain_run_is_unaffected_by_the_veto(self, tmp_path, monkeypatch, capsys):
        db_path, _ = self._seeded_db(tmp_path)
        monkeypatch.setattr(
            sender_module, "settings", dataclasses.replace(settings, outreach_dry_run=True)
        )

        exit_code = sender_module.main(["--db", str(db_path)])

        assert exit_code == 0
        assert "Refusing to send" not in capsys.readouterr().out


class TestNoRealEmail:
    def test_no_test_opens_a_real_smtp_connection(self, conn):
        """
        The autouse fixture in this module replaces smtplib.SMTP and
        SMTP_SSL with a function that raises. This test proves the guard is
        actually armed.
        """
        with pytest.raises(AssertionError, match="real SMTP connection"):
            smtplib.SMTP("smtp.example.invalid")

    def test_every_recipient_used_in_tests_is_unroutable(self, conn):
        """.invalid can never resolve (RFC 2606), so even a bug that reached
        a real mail server could not deliver anywhere."""
        company_id = _company(conn)
        message = get_message(conn, _approved(conn, company_id))
        assert message["recipient_email"].endswith(".invalid")
