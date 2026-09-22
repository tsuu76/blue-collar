"""
End-to-end proof of the automatic outreach path, with no human in the loop:

    company -> real posting -> discovered contact -> research -> personalized
    email -> quality gate PASS -> automatic SMTP send -> SENT recorded

Nothing here can reach a real mailbox. Every address is on a .invalid domain
(RFC 2606, reserved precisely so it can never resolve), the HTTP fetcher is a
scripted fake, and delivery goes through a RecordingTransport that stores the
EmailMessage instead of opening a socket. No test in this file constructs an
SmtpTransport.

OUTREACH_DRY_RUN is true by default, which is why each sending test patches a
copy of the settings object: turning sending on has to be explicit even here.
"""
from __future__ import annotations

import dataclasses
import smtplib

import pytest

from src.database.models import OutreachStatus
from src.database.outreach_repo import (
    get_company,
    get_message,
    insert_company,
    insert_message,
    list_messages_for_company,
    set_do_not_contact,
)
from src.outreach import pipeline as pipeline_module
from src.outreach import sender as sender_module
from src.outreach.pipeline import Outcome, run_outreach
from src.outreach.sender import EmailTransport

# The fixtures and fakes for a full pipeline run already exist; reusing them
# keeps this file about sending rather than about scaffolding.
from tests.test_outreach_pipeline import (  # noqa: F401 — imported as pytest fixtures
    ANALYSIS_JSON,
    GOOD_EMAIL,
    adapter,
    config,
    conn,
    db_path,
    resume,
)
from tests.fakes import FakeAIProvider

CAREERS_PAGE = """
<html><body>
  <h1>Careers at Example Co</h1>
  <p>We're hiring. Email <a href="mailto:careers@example.invalid">careers@example.invalid</a>.</p>
  <p>Our talent lead is <a href="https://www.linkedin.com/in/dana-example">Dana Example</a>.</p>
</body></html>
"""


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def make_fetcher(pages):
    def _fetch(url, timeout=None, **kwargs):
        if url not in pages:
            return FakeResponse(status_code=404)
        return FakeResponse(pages[url])

    return _fetch


class RecordingTransport(EmailTransport):
    """Accepts everything and keeps it. Opens no socket."""

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class FailingTransport(EmailTransport):
    """A mail server that refuses. One per failure mode worth proving."""

    def __init__(self, exc=None, fail_for=None):
        self.exc = exc or smtplib.SMTPRecipientsRefused({})
        self.fail_for = fail_for
        self.attempted = []

    def send(self, message):
        self.attempted.append(message["To"])
        if self.fail_for is None or message["To"] in self.fail_for:
            raise self.exc


@pytest.fixture()
def sending_on(monkeypatch):
    """
    Turn sending on for one test: OUTREACH_DRY_RUN off, SMTP 'configured'.

    Settings is a frozen dataclass, so this patches the module-level name in
    each consuming module at a modified copy — the same technique
    tests/conftest.py uses for notifications.
    """
    def _apply(**overrides):
        live = dataclasses.replace(
            pipeline_module.settings,
            outreach_dry_run=False,
            smtp_host="smtp.example.invalid",
            smtp_from_email="candidate@example.invalid",
            smtp_from_name="Test Candidate",
            smtp_username="",
            smtp_password="",
            **overrides,
        )
        monkeypatch.setattr(pipeline_module, "settings", live)
        monkeypatch.setattr(sender_module, "settings", live)
        return live

    return _apply


@pytest.fixture()
def run(db_path, config, resume, adapter):
    """
    A full pipeline run with a fake ATS adapter, fake AI, a fake HTTP
    fetcher, and whatever transport the test supplies.

    The default config entry has NO contact_email, so every run here has to
    discover one from the fake careers page — which is the path being proved.
    """
    def _run(*, entries=None, pages=None, email_text=None, **kwargs):
        if entries is None:
            entries = [
                {
                    "company": "Example Co",
                    "website": "https://example.invalid",
                    "platform": "greenhouse",
                    "identifier": "exampleco",
                }
            ]
        if pages is None:
            pages = {"https://example.invalid/careers": CAREERS_PAGE}
        return run_outreach(
            db_path=db_path,
            config_path=config(entries),
            resume=resume,
            analysis_provider=FakeAIProvider(json_responses=[ANALYSIS_JSON] * 5),
            email_provider=FakeAIProvider(
                text_responses=[email_text if email_text is not None else GOOD_EMAIL] * 5
            ),
            adapters={"greenhouse": adapter},
            employers=[],
            fetch=make_fetcher(pages),
            **kwargs,
        )

    return _run


# --------------------------------------------------------------------------
# The whole path, unattended
# --------------------------------------------------------------------------

class TestTheAutomaticPath:
    def test_company_to_sent_with_no_approval_step(self, run, conn, sending_on):
        """
        The headline case. One call, no human input, and the email is
        delivered — with every intermediate artifact recorded.
        """
        sending_on()
        transport = RecordingTransport()
        result = run(transport=transport)

        outcome = result.outcomes[0]
        assert outcome.outcome == Outcome.SENT, outcome.reason
        assert result.sent == 1
        assert result.failed == 0
        assert result.sending_enabled is True

        # The contact was discovered, not configured.
        company = get_company(conn, outcome.company_id)
        assert company["contact_email"] == "careers@example.invalid"
        assert company["contact_source"] == "careers_page"
        assert company["contact_evidence_url"] == "https://example.invalid/careers"

        # The research really happened and fed the email.
        import json

        research = json.loads(company["research_json"])
        assert research["posting_count"] == 1
        assert "Python" in research["skills"]

        # The message passed the gate and is recorded as sent.
        message = get_message(conn, outcome.message_id)
        assert message["gate_passed"] == 1
        assert message["status"] == OutreachStatus.SENT
        assert message["sent_at"] is not None
        assert message["send_attempts"] == 1
        assert message["last_error"] is None
        assert message["recipient_email"] == "careers@example.invalid"

        # And the email that actually went out is the drafted one.
        assert len(transport.messages) == 1
        delivered = transport.messages[0]
        assert delivered["To"] == "careers@example.invalid"
        assert delivered["Subject"] == message["subject"]
        assert delivered.get_content().strip() == message["body"].strip()

    def test_the_email_is_personalized_from_the_real_posting(self, run, conn, sending_on):
        """
        Not a template: the delivered body names things the company's own
        posting actually asked for.
        """
        sending_on()
        transport = RecordingTransport()
        run(transport=transport)
        body = transport.messages[0].get_content()
        assert "Example Co" in body
        assert "Service Desk Analyst" in body
        assert "Python" in body

    def test_the_linkedin_profile_is_stored_but_never_messaged(self, run, conn, sending_on):
        """
        LinkedIn is for the user to act on by hand. It is recorded on the
        company and appears nowhere in what was transmitted.
        """
        sending_on()
        transport = RecordingTransport()
        result = run(transport=transport)
        company = get_company(conn, result.outcomes[0].company_id)
        assert company["linkedin_url"] == "https://www.linkedin.com/in/dana-example"
        assert "linkedin" not in transport.messages[0].get_content().lower()

    def test_the_run_result_is_serializable(self, run, sending_on):
        import json

        sending_on()
        payload = json.loads(json.dumps(run(transport=RecordingTransport()).to_dict()))
        assert payload["sent"] == 1
        assert payload["sending_enabled"] is True


# --------------------------------------------------------------------------
# The safety rails, each proved to still hold
# --------------------------------------------------------------------------

class TestDryRunVeto:
    def test_the_veto_stops_the_send_but_not_the_draft(self, run, conn):
        """
        OUTREACH_DRY_RUN defaults true. Everything runs; nothing is sent; the
        gated draft is kept.
        """
        transport = RecordingTransport()
        result = run(transport=transport)
        assert result.sent == 0
        assert result.drafted == 1
        assert transport.messages == []

        message = get_message(conn, result.outcomes[0].message_id)
        assert message["status"] == OutreachStatus.DRAFT
        assert message["gate_passed"] == 1
        assert message["sent_at"] is None
        assert message["send_attempts"] == 0

    def test_the_pipeline_dry_run_flag_also_prevents_sending(self, run, sending_on):
        """A --dry-run rolls back; a send cannot be rolled back, so it must
        not happen."""
        sending_on()
        transport = RecordingTransport()
        result = run(transport=transport, dry_run=True)
        assert result.sent == 0
        assert transport.messages == []
        assert "--dry-run" in result.sending_blocked_reason


class TestUnverifiedRecipients:
    def test_a_company_with_no_published_address_is_skipped(self, run, conn, sending_on):
        """No address anywhere — no email, and no guess."""
        sending_on()
        transport = RecordingTransport()
        result = run(transport=transport, pages={})

        outcome = result.outcomes[0]
        assert outcome.outcome == Outcome.SKIPPED_NO_CONTACT_EMAIL
        assert outcome.message_id is None
        assert transport.messages == []
        assert list_messages_for_company(conn, outcome.company_id) == []

    def test_a_recruiter_name_alone_never_produces_a_send(self, run, conn, sending_on):
        """
        The page names a person and links their profile but publishes no
        address. Nothing is sent; the profile is stored for manual use.
        """
        sending_on()
        transport = RecordingTransport()
        page = (
            "<p>Talk to our hiring manager Dana Example.</p>"
            '<a href="https://www.linkedin.com/in/dana-example">Dana Example</a>'
        )
        result = run(
            transport=transport, pages={"https://example.invalid/careers": page}
        )
        outcome = result.outcomes[0]
        assert outcome.outcome == Outcome.SKIPPED_NO_CONTACT_EMAIL
        assert transport.messages == []
        company = get_company(conn, outcome.company_id)
        assert company["contact_email"] is None
        assert company["linkedin_url"] == "https://www.linkedin.com/in/dana-example"

    def test_a_discovered_address_never_overwrites_a_configured_one(
        self, run, conn, sending_on
    ):
        """
        An address the user typed outranks anything found on a page — a
        scrape must not be able to redirect their outreach.
        """
        sending_on()
        transport = RecordingTransport()
        result = run(
            transport=transport,
            entries=[
                {
                    "company": "Example Co",
                    "website": "https://example.invalid",
                    "platform": "greenhouse",
                    "identifier": "exampleco",
                    "contact_email": "known-good@example.invalid",
                }
            ],
        )
        assert result.outcomes[0].outcome == Outcome.SENT
        assert transport.messages[0]["To"] == "known-good@example.invalid"


class TestTheGateStillBlocks:
    def test_a_fabricated_claim_is_never_sent(self, run, conn, sending_on):
        """
        The deterministic gate is what stands between a hallucinating model
        and a real employer. An email claiming experience the master resume
        does not contain must not reach any transport, even with sending
        fully enabled.
        """
        sending_on()
        transport = RecordingTransport()
        fabricated = (
            "Subject: Senior engineer available\n\n"
            "Hi there, I have eight years of professional Kubernetes experience at Google "
            "and hold a CISSP certification. I led a team of twelve engineers building "
            "distributed systems in Go, and I have shipped production Terraform across "
            "three continents. I am AWS Solutions Architect certified and have a Master's "
            "degree in distributed computing. I would love to bring that depth to your "
            "team, and I am available to start immediately in a principal-level role. "
            "Thanks for your time, Test"
        )
        result = run(transport=transport, email_text=fabricated)

        outcome = result.outcomes[0]
        assert outcome.outcome in (Outcome.FAILED_GATE, Outcome.FAILED_DRAFT), outcome.reason
        assert transport.messages == []
        if outcome.message_id is not None:
            message = get_message(conn, outcome.message_id)
            assert message["gate_passed"] != 1
            assert message["status"] != OutreachStatus.SENT
            assert message["sent_at"] is None

    def test_a_gate_failure_reaches_no_transport(self, run, conn, sending_on, monkeypatch):
        """
        The wiring, proved directly: when the gate returns FAIL, the pipeline
        stops there. The draft is kept with gate_passed = 0 — readable and
        fixable — and no transport is ever handed anything.
        """
        sending_on()
        transport = RecordingTransport()

        real_gate = pipeline_module.gate_and_record

        def failing_gate(conn_, message_id, resume_):
            """Record a genuine FAIL, exactly as the real gate would."""
            from src.database import outreach_repo
            from src.outreach.quality_gate import CheckResult

            result = real_gate(conn_, message_id, resume_)
            result.checks.append(
                CheckResult(name="forced", passed=False, reason="forced failure for this test")
            )
            outreach_repo.save_gate_result(
                conn_, message_id, passed=result.passed, reasons=result.reasons
            )
            return result

        monkeypatch.setattr(pipeline_module, "gate_and_record", failing_gate)
        result = run(transport=transport)

        outcome = result.outcomes[0]
        assert outcome.outcome == Outcome.FAILED_GATE
        assert transport.messages == []

        message = get_message(conn, outcome.message_id)
        assert message["gate_passed"] == 0
        assert message["status"] == OutreachStatus.DRAFT
        assert message["sent_at"] is None
        assert message["send_attempts"] == 0

    def test_approve_message_still_guards_the_send(self, conn):
        """
        The structural guard, tested directly: a message that never passed
        the gate cannot be promoted, so it can never enter the send path.
        """
        from src.database.outreach_repo import approve_message

        company_id = insert_company(
            conn, {"name": "Gate Co", "contact_email": "careers@gate.invalid"}
        )
        message_id = insert_message(
            conn,
            {
                "company_id": company_id,
                "recipient_email": "careers@gate.invalid",
                "subject": "Hello",
                "body": "Body",
            },
        )
        assert approve_message(conn, message_id) is False
        assert get_message(conn, message_id)["status"] == OutreachStatus.DRAFT


class TestAlreadyContactedAndDuplicates:
    def test_a_company_already_emailed_is_not_emailed_again(self, run, conn, sending_on):
        sending_on()
        transport = RecordingTransport()
        first = run(transport=transport)
        assert first.outcomes[0].outcome == Outcome.SENT

        second = run(transport=transport)
        assert second.outcomes[0].outcome == Outcome.SKIPPED_ALREADY_CONTACTED
        assert len(transport.messages) == 1

    def test_a_pending_draft_blocks_a_second_draft(self, run, conn):
        """Under the veto the first run leaves a DRAFT; the second must not
        write another one for the same company."""
        first = run(transport=RecordingTransport())
        second = run(transport=RecordingTransport())
        assert second.outcomes[0].outcome == Outcome.SKIPPED_PENDING_MESSAGE
        assert len(list_messages_for_company(conn, first.outcomes[0].company_id)) == 1

    def test_a_draft_written_under_the_veto_is_sent_once_the_veto_lifts(
        self, run, conn, sending_on
    ):
        """
        The continuity case. A run with sending off leaves a gated draft; the
        next run, with sending on, delivers that same message rather than
        skipping it as "pending" or writing a second one.
        """
        first = run(transport=RecordingTransport())
        assert first.outcomes[0].outcome == Outcome.DRAFTED
        message_id = first.outcomes[0].message_id

        sending_on()
        transport = RecordingTransport()
        second = run(transport=transport)

        assert second.outcomes[0].outcome == Outcome.SENT
        assert second.outcomes[0].message_id == message_id
        assert len(transport.messages) == 1

        # Still exactly one message for this company — nothing was rewritten.
        messages = list_messages_for_company(conn, first.outcomes[0].company_id)
        assert len(messages) == 1
        assert messages[0]["status"] == OutreachStatus.SENT

    def test_a_gate_failed_draft_is_never_resumed(self, run, conn, sending_on, monkeypatch):
        """
        The same continuity path must not become a way for a rejected email
        to reach a mailbox later. A pending message that never passed the
        gate stays put.
        """
        from src.database import outreach_repo

        first = run(transport=RecordingTransport())
        message_id = first.outcomes[0].message_id
        outreach_repo.save_gate_result(
            conn, message_id, passed=False, reasons=["claims something unsupported"]
        )
        conn.commit()

        sending_on()
        transport = RecordingTransport()
        second = run(transport=transport)

        assert second.outcomes[0].outcome == Outcome.SKIPPED_PENDING_MESSAGE
        assert transport.messages == []
        assert get_message(conn, message_id)["status"] == OutreachStatus.DRAFT

    def test_a_do_not_contact_company_is_never_sent_to(self, run, conn, sending_on):
        """An opt-out is checked before any work is done, and blocks the run
        outright even with sending fully enabled."""
        sending_on()
        company_id = insert_company(
            conn, {"name": "Example Co", "website": "https://example.invalid"}
        )
        set_do_not_contact(conn, company_id, "they asked not to be contacted")
        conn.commit()

        transport = RecordingTransport()
        result = run(transport=transport)
        assert result.outcomes[0].outcome == Outcome.SKIPPED_DO_NOT_CONTACT
        assert transport.messages == []
        assert list_messages_for_company(conn, company_id) == []


class TestFailuresDoNotStopTheBatch:
    def test_one_refused_mailbox_does_not_block_the_others(self, run, conn, sending_on):
        """
        Three companies, the middle one refused by the mail server. The other
        two are delivered, and the failure is recorded on its own message.
        """
        sending_on(outreach_daily_limit=10)
        entries = [
            {
                "company": name,
                "website": f"https://{slug}.invalid",
                "platform": "greenhouse",
                "identifier": slug,
            }
            for name, slug in [("A Co", "aco"), ("B Co", "bco"), ("C Co", "cco")]
        ]
        pages = {
            f"https://{slug}.invalid/careers": (
                f'<a href="mailto:careers@{slug}.invalid">Jobs</a>'
            )
            for slug in ["aco", "bco", "cco"]
        }
        transport = FailingTransport(
            exc=smtplib.SMTPServerDisconnected("server went away"),
            fail_for={"careers@bco.invalid"},
        )
        result = run(transport=transport, entries=entries, pages=pages)

        by_company = {o.company: o for o in result.outcomes}
        assert by_company["A Co"].outcome == Outcome.SENT
        assert by_company["B Co"].outcome == Outcome.FAILED_SEND
        assert by_company["C Co"].outcome == Outcome.SENT
        assert result.sent == 2
        assert result.failed == 1
        # All three were attempted — the failure did not end the run.
        assert len(transport.attempted) == 3

    def test_a_failed_send_is_recorded_not_lost(self, run, conn, sending_on):
        sending_on()
        transport = FailingTransport(exc=smtplib.SMTPRecipientsRefused({}))
        result = run(transport=transport)

        message = get_message(conn, result.outcomes[0].message_id)
        assert message["status"] == OutreachStatus.FAILED
        assert message["sent_at"] is None
        assert message["send_attempts"] == 1
        assert "SMTPRecipientsRefused" in message["last_error"]

    def test_sent_is_never_recorded_before_smtp_succeeds(self, run, conn, sending_on):
        """
        The ordering guarantee. At the moment the transport is invoked the
        attempt must already be recorded but the message must NOT yet be
        marked sent — only a clean return from the mail server does that.
        """
        sending_on()
        observed = {}

        class InspectingTransport(EmailTransport):
            def __init__(self, message_id):
                self.message_id = message_id

            def send(self, message):
                row = get_message(conn, self.message_id)
                observed["status"] = row["status"]
                observed["sent_at"] = row["sent_at"]
                observed["attempts"] = row["send_attempts"]

        # Produce a gated draft first (this run cannot send: no live
        # transport reaches it because the veto fixture is applied per-call).
        from src.database import outreach_repo
        from src.outreach.sender import send_one

        first = run(transport=RecordingTransport(), pages={})
        company_id = insert_company(
            conn, {"name": "Order Co", "contact_email": "careers@order.invalid"}
        )
        message_id = insert_message(
            conn,
            {
                "company_id": company_id,
                "recipient_email": "careers@order.invalid",
                "subject": "Hello",
                "body": "Body",
            },
        )
        outreach_repo.save_gate_result(conn, message_id, passed=True, reasons=[])
        assert outreach_repo.approve_message(conn, message_id) is True
        conn.commit()

        send_one(conn, message_id, InspectingTransport(message_id), dry_run=False)

        assert observed["status"] == OutreachStatus.APPROVED
        assert observed["sent_at"] is None
        assert observed["attempts"] == 1

        after = get_message(conn, message_id)
        assert after["status"] == OutreachStatus.SENT
        assert after["sent_at"] is not None


class TestDailyLimit:
    def test_the_limit_caps_a_run(self, run, conn, sending_on):
        sending_on(outreach_daily_limit=2)
        entries = [
            {
                "company": name,
                "website": f"https://{slug}.invalid",
                "platform": "greenhouse",
                "identifier": slug,
            }
            for name, slug in [("A Co", "aco"), ("B Co", "bco"), ("C Co", "cco")]
        ]
        pages = {
            f"https://{slug}.invalid/careers": f'<a href="mailto:careers@{slug}.invalid">Jobs</a>'
            for slug in ["aco", "bco", "cco"]
        }
        transport = RecordingTransport()
        result = run(transport=transport, entries=entries, pages=pages)

        assert result.sent == 2
        assert len(transport.messages) == 2
        assert result.outcomes[-1].outcome == Outcome.SKIPPED_DAILY_LIMIT

    def test_the_default_limit_is_five(self):
        from src.config import settings

        assert settings.outreach_daily_limit == 5


class TestSmtpConfiguration:
    def test_an_unconfigured_mailbox_drafts_instead_of_failing_everything(
        self, run, conn, monkeypatch
    ):
        """
        Missing SMTP settings would fail every message and mark them all
        FAILED. The run refuses to send instead, and keeps the drafts.
        """
        broken = dataclasses.replace(
            pipeline_module.settings, outreach_dry_run=False, smtp_host="", smtp_from_email=""
        )
        monkeypatch.setattr(pipeline_module, "settings", broken)
        monkeypatch.setattr(sender_module, "settings", broken)

        result = run()
        assert result.sent == 0
        assert result.drafted == 1
        assert "SMTP is not configured" in result.sending_blocked_reason
        message = get_message(conn, result.outcomes[0].message_id)
        assert message["status"] == OutreachStatus.DRAFT
