"""
Outreach orchestrator — the second pathway, start to finish, unattended:

    config/outreach_companies.json
      -> research        real, currently-advertised ATS postings
      -> contact         a legitimately published address for this company
      -> personalization what those postings say, verified against evidence
      -> draft           the email, verified against the master resume
      -> quality gate    deterministic PASS/FAIL
      -> SMTP send       automatically, if and only if the gate passed
      -> SENT recorded

There is no approval step. A draft that passes every check is sent by the
same run that wrote it, which is the whole point of this module — but
"passes every check" is doing real work, and none of those checks were
loosened to make automatic sending possible:

  - OUTREACH_DRY_RUN is an absolute veto. With it on, this pipeline still
    researches, drafts and gates, and then stops: no transport is built, no
    socket is opened, and the message is left as a DRAFT the fallback
    approve route can still handle by hand.
  - Nothing is sent to an address the company did not publish. contact
    discovery never constructs or guesses one (see contact_discovery.py).
  - approve_message still refuses any message whose gate_passed isn't 1, and
    send_one still re-verifies everything against the live database
    immediately before the socket opens. Both are called, not bypassed.
  - Already-contacted companies, companies with a pending message, and
    do-not-contact companies are all skipped before any work is done.
  - OUTREACH_DAILY_LIMIT caps the run, and is re-checked against the live
    sent-today count before each individual send.

Every actual step lives somewhere else and is called, not reimplemented:

  - targets.py            which real companies to consider
  - research.py           their real, currently-advertised postings
  - contact_discovery.py  a published address, or an honest "none found"
  - personalization.py    what those postings say, verified against evidence
  - email_draft.py        the email, verified against the master resume
  - quality_gate.py       the deterministic PASS/FAIL
  - sender.py             the only code in the project that opens SMTP
  - database/outreach_repo.py   storage, dedup and status rules

One company can never take down a run: every per-company failure — a dead
website, an AI error, a refused mailbox — is caught, recorded against that
company, and the next one is processed.

Run it with:  python -m src.outreach.pipeline [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from src.ai.base import AIProvider, AIResponseError
from src.config import settings
from src.database import outreach_repo
from src.database.db import get_connection, init_db
from src.database.models import OutreachStatus
from src.job_discovery.base import JobDiscoverySource
from src.resume.schema import MasterResume
from src.resume.store import load_master_resume

from .contact_discovery import discover_contact
from .email_draft import draft_outreach_email
from .personalization import analyze_company
from .quality_gate import gate_and_record
from .research import research_company
from .sender import (
    DryRunTransport,
    EmailTransport,
    SmtpTransport,
    send_one,
    smtp_configuration_problems,
)
from .targets import OutreachTarget, enabled_targets

logger = logging.getLogger("job_hunter.outreach.pipeline")


class Outcome:
    """What happened to one company in a run."""

    # The success case: written, gated, and accepted by the mail server.
    SENT = "SENT"
    # Written and gated, but deliberately not sent — OUTREACH_DRY_RUN is on,
    # or SMTP isn't configured. The draft is kept, not discarded.
    DRAFTED = "DRAFTED"
    SKIPPED_DO_NOT_CONTACT = "SKIPPED_DO_NOT_CONTACT"
    SKIPPED_PENDING_MESSAGE = "SKIPPED_PENDING_MESSAGE"
    SKIPPED_ALREADY_CONTACTED = "SKIPPED_ALREADY_CONTACTED"
    SKIPPED_NO_POSTINGS = "SKIPPED_NO_POSTINGS"
    SKIPPED_NO_CONTACT_EMAIL = "SKIPPED_NO_CONTACT_EMAIL"
    SKIPPED_NO_OVERLAP = "SKIPPED_NO_OVERLAP"
    SKIPPED_DAILY_LIMIT = "SKIPPED_DAILY_LIMIT"
    # A pre-send check refused the message (see sender.verify_sendable).
    # Fail-safe, not an error: nothing was transmitted and nothing broke.
    SKIPPED_SEND_BLOCKED = "SKIPPED_SEND_BLOCKED"
    FAILED_ANALYSIS = "FAILED_ANALYSIS"
    FAILED_DRAFT = "FAILED_DRAFT"
    # Draft was written but did not survive the final deterministic gate.
    # It stays in the database, unapprovable, with its reasons recorded.
    FAILED_GATE = "FAILED_GATE"
    # The mail server rejected it, or the connection failed. Recorded on the
    # message (status FAILED, last_error set); the run continues.
    FAILED_SEND = "FAILED_SEND"
    # Anything unforeseen in one company's processing. Caught so that a
    # single bad target cannot end the batch.
    FAILED_UNEXPECTED = "FAILED_UNEXPECTED"


@dataclass
class CompanyOutcome:
    company: str
    outcome: str
    reason: str = ""
    company_id: int | None = None
    message_id: int | None = None

    def to_dict(self) -> dict:
        return {
            "company": self.company,
            "outcome": self.outcome,
            "reason": self.reason or None,
            "company_id": self.company_id,
            "message_id": self.message_id,
        }


@dataclass
class OutreachRunResult:
    targets_considered: int = 0
    sent: int = 0
    # Drafted but NOT sent — kept separate from `sent` on purpose, so a run
    # can never report a send it did not make.
    drafted: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: bool = False
    daily_limit: int = 0
    budget_remaining: int = 0
    # Whether this run was allowed to open SMTP at all, and why not.
    sending_enabled: bool = False
    sending_blocked_reason: str = ""
    outcomes: list[CompanyOutcome] = field(default_factory=list)

    def add(self, outcome: CompanyOutcome) -> CompanyOutcome:
        self.outcomes.append(outcome)
        if outcome.outcome == Outcome.SENT:
            self.sent += 1
        elif outcome.outcome == Outcome.DRAFTED:
            self.drafted += 1
        elif outcome.outcome.startswith("FAILED"):
            self.failed += 1
        else:
            self.skipped += 1
        return outcome

    def to_dict(self) -> dict:
        return {
            "targets_considered": self.targets_considered,
            "sent": self.sent,
            "drafted": self.drafted,
            "skipped": self.skipped,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "daily_limit": self.daily_limit,
            "budget_remaining": self.budget_remaining,
            "sending_enabled": self.sending_enabled,
            "sending_blocked_reason": self.sending_blocked_reason or None,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def sending_status(dry_run: bool, transport: EmailTransport | None) -> tuple[bool, str]:
    """
    Decide whether this run may transmit anything, and say why if not.

    Three independent conditions, checked in order of authority:

      1. OUTREACH_DRY_RUN. The environment veto, and the reason it is a veto
         rather than a default: nothing in this call — no argument, no
         button, no injected transport — can turn sending on while it is
         true. An injected test transport is refused along with everything
         else, so a test can never accidentally prove that the veto leaks.
      2. The run's own --dry-run, which rolls the transaction back at the
         end. Sending is not rollbackable, so it must not happen here.
      3. SMTP configuration. Missing settings would fail every message and
         mark them all FAILED; refusing up front leaves them as drafts.
    """
    if settings.outreach_dry_run:
        return False, (
            "OUTREACH_DRY_RUN is true — drafts were written and gated, but nothing was sent. "
            "Set OUTREACH_DRY_RUN=false in .env to enable sending."
        )
    if dry_run:
        return False, "this was a --dry-run — nothing was written or sent"
    if transport is None:
        problems = smtp_configuration_problems()
        if problems:
            return False, "SMTP is not configured: " + "; ".join(problems)
    return True, ""


def run_outreach(
    *,
    db_path: str | Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    resume: MasterResume | None = None,
    analysis_provider: AIProvider | None = None,
    email_provider: AIProvider | None = None,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
    employers: list | None = None,
    transport: EmailTransport | None = None,
    fetch=None,
) -> OutreachRunResult:
    """
    Research every enabled outreach target, write an email for the ones that
    genuinely qualify, and send it — all in one pass, with no approval step.

    dry_run runs the complete pipeline — real research, real AI, real
    verification — and then rolls the transaction back, so nothing is
    persisted and nothing is sent. Useful for seeing exactly what a run would
    produce before letting it act.

    limit caps how many messages this run creates, on top of the configured
    daily budget (see _daily_budget).

    The provider/adapter/resume/transport/fetch arguments are
    dependency-injection hooks for tests, mirroring how
    src/pipeline/process_job.py takes its providers. An injected transport
    does NOT override OUTREACH_DRY_RUN — see sending_status.
    """
    targets = [t for t in enabled_targets(config_path) if t.company]
    result = OutreachRunResult(targets_considered=len(targets), dry_run=dry_run)

    if not targets:
        logger.info("No enabled outreach targets configured — nothing to do.")
        return result

    if resume is None:
        resume = load_master_resume()

    sending_enabled, blocked_reason = sending_status(dry_run, transport)
    if sending_enabled and transport is None:
        transport = SmtpTransport()
    if not sending_enabled:
        # Never leave a live transport lying around on a run that must not
        # send. DryRunTransport opens no socket even if something reached it.
        transport = DryRunTransport()
        logger.info("[OUTREACH] Sending is OFF: %s", blocked_reason)

    conn = get_connection(db_path)
    try:
        budget = _daily_budget(conn, limit)
        result.daily_limit = settings.outreach_daily_limit
        result.budget_remaining = budget
        result.sending_enabled = sending_enabled
        result.sending_blocked_reason = blocked_reason

        for target in targets:
            if budget <= 0:
                result.add(
                    CompanyOutcome(
                        target.company,
                        Outcome.SKIPPED_DAILY_LIMIT,
                        f"daily outreach limit reached ({settings.outreach_daily_limit})",
                    )
                )
                continue

            try:
                outcome = _process_target(
                    conn,
                    target,
                    resume,
                    analysis_provider=analysis_provider,
                    email_provider=email_provider,
                    adapters=adapters,
                    employers=employers,
                    transport=transport,
                    sending_enabled=sending_enabled,
                    sending_blocked_reason=blocked_reason,
                    fetch=fetch,
                )
            except Exception as exc:  # noqa: BLE001
                # One company must never end the batch. Roll back whatever
                # half-written state this target left, record it, move on.
                logger.exception("Outreach failed unexpectedly for %r", target.company)
                conn.rollback()
                outcome = CompanyOutcome(
                    target.company, Outcome.FAILED_UNEXPECTED, f"{type(exc).__name__}: {exc}"
                )

            result.add(outcome)
            # The budget counts messages created, whatever became of them —
            # a draft that failed the gate still cost a slot, and counting
            # only successes would let a bad day loop past the daily cap.
            if outcome.message_id is not None:
                budget -= 1

            # Commit per company so one failure late in a long run doesn't
            # discard the drafts already produced — same per-item commit
            # rhythm run_discovery uses.
            if dry_run:
                continue
            conn.commit()

        result.budget_remaining = budget

        if dry_run:
            # Nothing this run did is kept. The research and AI calls really
            # happened; the database is left exactly as it was found.
            conn.rollback()
            logger.info("[OUTREACH] DRY RUN — rolled back, nothing was written.")
    finally:
        conn.close()

    logger.info(
        "[OUTREACH] Targets: %d | Sent: %d | Drafted (not sent): %d | Skipped: %d | "
        "Failed: %d | Dry run: %s",
        result.targets_considered, result.sent, result.drafted, result.skipped,
        result.failed, dry_run,
    )
    return result


def _daily_budget(conn: sqlite3.Connection, limit: int | None) -> int:
    """
    How many messages this run may create and send.

    One limit covers both, because with automatic sending they are the same
    act: a message created is a message sent. It counts what today has
    already produced — whichever of "drafted" or "sent" is higher — so two
    runs in one day produce at most `outreach_daily_limit` between them
    rather than that many each. An explicit `limit` narrows it further but
    can never widen it past the configured cap.

    This is the run-level budget. sender.send_one is still guarded
    separately by a live count immediately before each individual send, so a
    concurrent run cannot slip past the cap between these two checks.
    """
    already = max(outreach_repo.count_created_today(conn), outreach_repo.count_sent_today(conn))
    budget = max(0, settings.outreach_daily_limit - already)
    if limit is not None:
        budget = min(budget, max(0, limit))
    return budget


def _process_target(
    conn: sqlite3.Connection,
    target: OutreachTarget,
    resume: MasterResume,
    *,
    analysis_provider: AIProvider | None,
    email_provider: AIProvider | None,
    adapters: Mapping[str, JobDiscoverySource] | None,
    employers: list | None,
    transport: EmailTransport,
    sending_enabled: bool,
    sending_blocked_reason: str = "",
    fetch=None,
) -> CompanyOutcome:
    """
    One company, start to finish — researched, drafted, gated and sent.

    Guards run cheapest-first: database checks before any network call, the
    network calls before the AI calls, and the send last of all. A company
    that was never going to be contacted costs nothing, and one with no
    published address costs two small GETs rather than a full AI run.
    """
    company_id = outreach_repo.upsert_company(conn, target.to_company_record())
    company = outreach_repo.get_company(conn, company_id)
    outcome = lambda status, reason="", message_id=None: CompanyOutcome(  # noqa: E731
        target.company, status, reason, company_id, message_id
    )

    # --- Free checks: has this company already been handled? ---------------
    if outreach_repo.is_do_not_contact(conn, company_id):
        return outcome(
            Outcome.SKIPPED_DO_NOT_CONTACT,
            company["do_not_contact_reason"] or "marked do-not-contact",
        )
    if outreach_repo.has_been_contacted(conn, company_id):
        return outcome(Outcome.SKIPPED_ALREADY_CONTACTED, "this company has already been emailed")
    # The idempotency guard: an existing DRAFT or APPROVED message means a
    # re-run never writes this company a second email.
    #
    # It does not mean the run ignores it. A message written while sending
    # was off (OUTREACH_DRY_RUN on, SMTP unconfigured, daily limit reached)
    # is finished work waiting for a run that is allowed to send — so if this
    # is that run, it is delivered here rather than being stranded as
    # "pending" forever.
    pending = _pending_message(conn, company_id)
    if pending is not None:
        if sending_enabled and pending["gate_passed"] == 1:
            return _send_existing(
                conn, pending, transport, outcome, company_name=target.company
            )
        return outcome(
            Outcome.SKIPPED_PENDING_MESSAGE,
            "an email for this company was already written and is waiting to be sent",
            message_id=pending["id"],
        )

    # --- Network: the company's own public job board -----------------------
    research = research_company(
        target.company,
        target.platform,
        target.identifier,
        employers=employers,
        adapters=adapters,
    )
    # Store what was found either way, so the dashboard can explain a company
    # that turned out to have nothing to say.
    outreach_repo.save_company_research(conn, company_id, research.summary())

    if not research.has_postings:
        return outcome(
            Outcome.SKIPPED_NO_POSTINGS,
            research.error or "no current public postings found on this company's job board",
        )

    # --- Who to email --------------------------------------------------
    # Run after research, so a company that isn't hiring never causes its
    # website to be read, and before any AI call, so an unreachable company
    # never costs a generation. contact_discovery only ever returns an
    # address the company itself published — it never builds one from a
    # name, and it never raises.
    contact = discover_contact(
        company, research, **({"fetch": fetch} if fetch is not None else {})
    )
    outreach_repo.save_discovered_contact(
        conn,
        company_id,
        email=contact.email,
        name=contact.name,
        source=contact.source,
        evidence_url=contact.evidence_url,
        linkedin_url=contact.linkedin_url,
        x_handle=contact.x_handle,
        x_name=contact.x_name,
        # Full picker inventory — every published address the search
        # found across the company's own pages and real ATS postings.
        # The sender still uses `contact_email` from the row; this
        # column just gives the dashboard something to offer as
        # alternates.
        all_addresses=contact.all_addresses,
    )
    # Re-read: save_discovered_contact refuses to overwrite an address that
    # was already stored, so the row is the authority on what will be used,
    # not the DiscoveredContact.
    company = outreach_repo.get_company(conn, company_id)

    if not (company["contact_email"] or "").strip():
        # A LinkedIn profile may have been found and stored. It is shown on
        # the dashboard for the user to act on by hand; nothing here messages
        # it, and it is never turned into an email address.
        return outcome(
            Outcome.SKIPPED_NO_CONTACT_EMAIL,
            contact.reason or "no verified contact email was found for this company",
        )

    # --- AI: analyse the real postings, verified against real evidence -----
    try:
        personalization = analyze_company(research, resume, provider=analysis_provider)
    except (AIResponseError, ValueError) as exc:
        logger.warning("Personalization failed for %r: %s", target.company, exc)
        return outcome(Outcome.FAILED_ANALYSIS, str(exc))

    if not personalization.has_overlap:
        return outcome(
            Outcome.SKIPPED_NO_OVERLAP,
            "nothing this company asks for overlaps with the resume — no honest email to write",
        )

    # --- AI: write the email, then verify it before it is stored -----------
    draft = draft_outreach_email(
        conn, company, research, resume, personalization, provider=email_provider
    )
    if not draft.ok:
        return outcome(
            Outcome.FAILED_DRAFT if draft.problems else Outcome.SKIPPED_PENDING_MESSAGE,
            draft.skipped_reason or "; ".join(draft.problems),
        )

    # --- Final deterministic gate -----------------------------------------
    # The draft exists, but it is not eligible for anything until this
    # passes: approve_message refuses a message whose gate_passed isn't 1.
    # A FAIL leaves the draft in place, unapprovable, with its reasons
    # recorded — so it can be read and edited rather than silently lost.
    gate = gate_and_record(conn, draft.message_id, resume)
    if not gate.passed:
        return outcome(
            Outcome.FAILED_GATE, "; ".join(gate.reasons), message_id=draft.message_id
        )

    # --- Automatic send ---------------------------------------------------
    # Everything above passed, so this message is eligible. Sending it is
    # still routed through the same two components the manual path used —
    # approve_message, which refuses anything not carrying a recorded gate
    # PASS, and send_one, which re-verifies against the live database and
    # only marks SENT after the mail server accepts it.
    if not sending_enabled:
        return outcome(
            Outcome.DRAFTED,
            sending_blocked_reason or "sending is disabled",
            message_id=draft.message_id,
        )
    return _send_now(conn, draft.message_id, transport, outcome)


def _pending_message(conn: sqlite3.Connection, company_id: int):
    """
    The company's one unfinished message, if it has one: a DRAFT or an
    APPROVED that was never delivered. Mirrors the statuses
    outreach_repo.has_pending_message counts, but returns the row so the
    caller can act on it.
    """
    for message in outreach_repo.list_messages_for_company(conn, company_id):
        if message["status"] in (OutreachStatus.DRAFT, OutreachStatus.APPROVED):
            return message
    return None


def _send_existing(conn, message, transport, outcome, *, company_name: str) -> CompanyOutcome:
    """Deliver a message a previous run wrote but was not allowed to send."""
    logger.info(
        "Sending message %d for %r, written by an earlier run.", message["id"], company_name
    )
    return _send_now(conn, message["id"], transport, outcome)


def _send_now(conn: sqlite3.Connection, message_id: int, transport, outcome) -> CompanyOutcome:
    """
    Clear one gated message for sending and hand it to the transport.

    Both steps are the existing ones: approve_message refuses anything not
    carrying a recorded gate PASS, and send_one re-verifies every condition
    against the live database before opening a socket and only marks SENT
    after the mail server accepts. Neither is bypassed or reimplemented here.
    """
    # The daily cap, re-read live rather than trusted from the run budget:
    # another run may have used part of today's allowance since this one
    # started.
    if outreach_repo.count_sent_today(conn) >= settings.outreach_daily_limit:
        return outcome(
            Outcome.SKIPPED_DAILY_LIMIT,
            f"daily send limit reached ({settings.outreach_daily_limit}) — "
            "this email is kept and goes out on the next run",
            message_id=message_id,
        )

    message = outreach_repo.get_message(conn, message_id)
    if message is not None and message["status"] == OutreachStatus.DRAFT:
        if not outreach_repo.approve_message(conn, message_id):
            # Should be unreachable — the gate passed — but this is the
            # structural guard, so a surprise here means do not send.
            return outcome(
                Outcome.FAILED_SEND,
                "the message could not be cleared for sending; nothing was transmitted",
                message_id=message_id,
            )
        conn.commit()

    send = send_one(conn, message_id, transport, dry_run=False)
    if send.sent:
        return outcome(Outcome.SENT, f"sent to {send.recipient}", message_id=message_id)
    if send.error:
        return outcome(Outcome.FAILED_SEND, send.error, message_id=message_id)
    return outcome(
        Outcome.SKIPPED_SEND_BLOCKED,
        send.skipped_reason or "a pre-send check refused this message",
        message_id=message_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Research outreach companies, write verified emails for them, and send "
            "the ones that pass the quality gate. Sending requires OUTREACH_DRY_RUN=false; "
            "with it true, everything runs but nothing is transmitted."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the whole pipeline, then roll back — nothing is written to the database.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap drafts created this run (within the daily limit)."
    )
    parser.add_argument("--db", default=None, help="Database path (defaults to DATABASE_PATH).")
    parser.add_argument("--config", default=None, help="Outreach registry path.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Apply any pending additive migrations, the same thing the dashboard
    # entry point does on startup. A database created before the contact
    # columns existed is otherwise a per-company failure on every target.
    init_db(args.db)

    result = run_outreach(
        db_path=args.db,
        config_path=Path(args.config) if args.config else None,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    print(
        f"\nTargets considered: {result.targets_considered}\n"
        f"Sent:               {result.sent}\n"
        f"Drafted, not sent:  {result.drafted}{'  (rolled back — dry run)' if result.dry_run else ''}\n"
        f"Skipped:            {result.skipped}\n"
        f"Failed:             {result.failed}\n"
        f"Daily limit:        {result.daily_limit} (remaining after this run: {result.budget_remaining})\n"
    )
    for outcome in result.outcomes:
        detail = f" — {outcome.reason}" if outcome.reason else ""
        print(f"  {outcome.outcome:<28} {outcome.company}{detail}")
    if not result.sending_enabled:
        print(f"\nNothing was sent: {result.sending_blocked_reason}")
    print("\nSee /outreach in the dashboard for the full picture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
