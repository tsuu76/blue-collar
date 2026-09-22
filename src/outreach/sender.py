"""
SMTP sending — the only module in this project that can transmit an email.

Standard library only (smtplib + email.message), through the user's own
mailbox. No transactional-email service, no API key, no new dependency,
consistent with the project's $0/month rule.

Everything here is built around one idea: **a message is not sent until the
mail server says it is.** Nothing is marked sent optimistically, and every
condition that made a message sendable is re-checked against the live
database immediately before the socket is opened — because approval could
have happened days ago, and a company can opt out in the meantime.

Order of operations per message, deliberately:

    re-verify (live)  ->  record attempt + COMMIT  ->  SMTP send
                      ->  mark sent + COMMIT       (only on success)
                      ->  mark failed + COMMIT     (on any exception)

Recording the attempt *before* the send, and committing it, means a crash
mid-delivery leaves visible evidence rather than a silent gap. Marking sent
only *after* SMTP returns means an interrupted run can never claim a
delivery that did not happen.

What this module will not do: approve anything, bypass the quality gate,
schedule itself, or send while OUTREACH_DRY_RUN is on. That setting is an
absolute veto on the command line — `--live` is refused while it is true,
rather than overriding it.

Run it with:  python -m src.outreach.sender [--limit N] [--live]
"""
from __future__ import annotations

import argparse
import logging
import smtplib
import sqlite3
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from src.config import settings
from src.database import outreach_repo
from src.database.db import get_connection
from src.database.models import OutreachStatus

logger = logging.getLogger("job_hunter.outreach.sender")


class SendBlocked(Exception):
    """Raised when a message must not be sent. Carries the reason."""


@dataclass
class SendResult:
    message_id: int
    company: str = ""
    recipient: str = ""
    sent: bool = False
    skipped_reason: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "company": self.company,
            "recipient": self.recipient,
            "sent": self.sent,
            "skipped_reason": self.skipped_reason or None,
            "error": self.error or None,
        }


@dataclass
class SendRunResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: bool = True
    daily_limit: int = 0
    already_sent_today: int = 0
    budget: int = 0
    results: list[SendResult] = field(default_factory=list)

    def add(self, result: SendResult) -> SendResult:
        self.results.append(result)
        if result.sent:
            self.sent += 1
        elif result.error:
            self.failed += 1
        else:
            self.skipped += 1
        return result

    def to_dict(self) -> dict:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
            "daily_limit": self.daily_limit,
            "already_sent_today": self.already_sent_today,
            "budget": self.budget,
            "results": [r.to_dict() for r in self.results],
        }


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class EmailTransport:
    """
    Anything that can deliver an EmailMessage. `send` must raise on failure
    and return normally ONLY when the mail server accepted the message —
    the caller treats a clean return as proof of delivery.
    """

    def send(self, message: EmailMessage) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SmtpTransport(EmailTransport):
    """
    Real delivery through the user's own mailbox, standard library only.

    Port 465 uses implicit TLS (SMTP_SSL); anything else connects plain and
    upgrades with STARTTLS before authenticating, so credentials are never
    put on the wire in the clear.
    """

    def __init__(self, host="", port=0, username="", password="", timeout=30):
        self.host = host or settings.smtp_host
        self.port = port or settings.smtp_port
        self.username = username or settings.smtp_username
        self.password = password or settings.smtp_password
        self.timeout = timeout

    def send(self, message: EmailMessage) -> None:
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as server:
                self._authenticate_and_send(server, message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                self._authenticate_and_send(server, message)

    def _authenticate_and_send(self, server, message: EmailMessage) -> None:
        if self.username:
            server.login(self.username, self.password)
        server.send_message(message)


class DryRunTransport(EmailTransport):
    """
    Opens no socket. Records what would have been sent so a dry run can be
    inspected, and is what OUTREACH_DRY_RUN selects.
    """

    def __init__(self):
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)
        logger.info(
            "[DRY RUN] Would send to %s — %r (nothing was transmitted).",
            message["To"], message["Subject"],
        )


# --------------------------------------------------------------------------
# Configuration + message building
# --------------------------------------------------------------------------

def smtp_configuration_problems() -> list[str]:
    """What's missing before a real send is possible. Empty list = ready."""
    problems = []
    if not settings.smtp_host:
        problems.append("SMTP_HOST is not set")
    if not settings.smtp_from_email:
        problems.append("SMTP_FROM_EMAIL is not set")
    if settings.smtp_username and not settings.smtp_password:
        problems.append("SMTP_USERNAME is set but SMTP_PASSWORD is empty")
    return problems


def build_email(message: sqlite3.Row) -> EmailMessage:
    """
    Assemble the outgoing email. Plain text only — no HTML, no tracking
    pixel, no attachment: this is meant to read like a person typed it.
    """
    email = EmailMessage()
    from_email = settings.smtp_from_email
    email["From"] = (
        formataddr((settings.smtp_from_name, from_email)) if settings.smtp_from_name else from_email
    )
    email["To"] = message["recipient_email"]
    email["Subject"] = message["subject"] or ""
    email.set_content(message["body"] or "")
    return email


# --------------------------------------------------------------------------
# Pre-send verification
# --------------------------------------------------------------------------

def verify_sendable(conn: sqlite3.Connection, message_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    """
    Re-check every condition against the live database, immediately before
    sending. Raises SendBlocked with a reason if anything fails.

    This repeats what list_approved_messages already filtered on, on purpose.
    That query may have run seconds or minutes ago, and in a long drain a
    company can be flagged do-not-contact, or a contact address corrected,
    while earlier messages are still going out.

    Checks are ordered so the reason names the root cause rather than a
    downstream symptom: an opted-out company sets its pending messages to
    DO_NOT_CONTACT, and a sent message's status becomes SENT, so testing
    status first would report "status is SENT" instead of the far more
    useful "already sent".
    """
    message = outreach_repo.get_message(conn, message_id)
    if message is None:
        raise SendBlocked(f"message {message_id} no longer exists")

    if message["sent_at"] is not None:
        raise SendBlocked("this message has already been sent")

    company = outreach_repo.get_company(conn, message["company_id"])
    if company is None:
        raise SendBlocked("the company record for this message is missing")
    if company["do_not_contact"]:
        raise SendBlocked(
            company["do_not_contact_reason"] or "this company is marked do-not-contact"
        )

    if message["status"] != OutreachStatus.APPROVED:
        raise SendBlocked(f"status is {message['status']}, not APPROVED")
    if message["gate_passed"] != 1:
        raise SendBlocked("the quality gate has not passed this message")

    # The recipient re-check: the address on the message must still be the
    # company's verified contact address. Anything else means the message
    # was edited or the company's address changed since approval, and this
    # email would go somewhere nobody verified.
    recipient = (message["recipient_email"] or "").strip()
    verified = (company["contact_email"] or "").strip()
    if not recipient:
        raise SendBlocked("the message has no recipient address")
    if not verified:
        raise SendBlocked("the company no longer has a verified contact address")
    if recipient.lower() != verified.lower():
        raise SendBlocked(
            f"recipient {recipient!r} is not the company's verified address {verified!r}"
        )

    return message, company


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def send_one(
    conn: sqlite3.Connection,
    message_id: int,
    transport: EmailTransport,
    *,
    dry_run: bool = True,
) -> SendResult:
    """
    Send exactly one approved message. Never raises: a blocked message or a
    mail-server failure comes back on the result.

    The caller does not need to commit — this commits at each step itself,
    because a send is irreversible and its record must not be sitting in an
    uncommitted transaction when the next one is attempted.
    """
    result = SendResult(message_id=message_id)

    try:
        message, company = verify_sendable(conn, message_id)
    except SendBlocked as exc:
        result.skipped_reason = str(exc)
        logger.warning("Not sending message %d: %s", message_id, exc)
        return result

    result.company = company["name"]
    result.recipient = message["recipient_email"]
    email = build_email(message)

    if dry_run:
        # Every check above has run. Nothing is transmitted and nothing in
        # the database changes — a dry run must leave no trace.
        transport.send(email)
        result.skipped_reason = "dry run — all checks passed, nothing was sent"
        return result

    # Count the attempt BEFORE the socket opens, and commit it, so an
    # interrupted delivery leaves evidence instead of a silent gap.
    outreach_repo.record_send_attempt(conn, message_id)
    conn.commit()

    try:
        transport.send(email)
    except Exception as exc:  # noqa: BLE001 — any delivery failure is a failure
        detail = f"{type(exc).__name__}: {exc}"
        outreach_repo.mark_message_failed(conn, message_id, detail)
        conn.commit()
        result.error = detail
        logger.warning("Send failed for %r (message %d): %s", result.company, message_id, detail)
        return result

    # Only now — the mail server accepted it.
    outreach_repo.mark_message_sent(conn, message_id)
    conn.commit()
    result.sent = True
    logger.info("Sent message %d to %s (%s).", message_id, result.recipient, result.company)
    return result


def send_approved(
    *,
    db_path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
    transport: EmailTransport | None = None,
    dry_run: bool | None = None,
    limit: int | None = None,
) -> SendRunResult:
    """
    Drain the approved queue, up to the day's remaining budget.

    dry_run defaults to OUTREACH_DRY_RUN, which itself defaults to true — so
    an accidental invocation transmits nothing. A real send has to be asked
    for explicitly.
    """
    if dry_run is None:
        dry_run = settings.outreach_dry_run

    owns_connection = conn is None
    conn = conn or get_connection(db_path)
    run = SendRunResult(dry_run=dry_run, daily_limit=settings.outreach_daily_limit)

    try:
        if not dry_run and transport is None:
            problems = smtp_configuration_problems()
            if problems:
                # Refuse rather than half-send: a misconfigured mailbox would
                # fail every message in the queue and mark them all FAILED.
                raise ValueError(
                    "Cannot send: " + "; ".join(problems) + ". Set these in .env first."
                )

        if transport is None:
            transport = DryRunTransport() if dry_run else SmtpTransport()

        run.already_sent_today = outreach_repo.count_sent_today(conn)
        budget = max(0, settings.outreach_daily_limit - run.already_sent_today)
        if limit is not None:
            budget = min(budget, max(0, limit))
        run.budget = budget

        if budget == 0:
            logger.info(
                "Daily outreach limit reached (%d/%d sent today) — nothing to send.",
                run.already_sent_today, settings.outreach_daily_limit,
            )
            return run

        # Fetch only as many as the budget allows, so the limit is enforced
        # by the query rather than by remembering to stop.
        queued = outreach_repo.list_approved_messages(conn, limit=budget)
        for row in queued:
            result = send_one(conn, row["id"], transport, dry_run=dry_run)
            run.add(result)
            # Re-check the live count each iteration: a concurrent run could
            # have consumed part of the day's budget while this one worked.
            if not dry_run and outreach_repo.count_sent_today(conn) >= settings.outreach_daily_limit:
                logger.info("Daily outreach limit reached mid-run — stopping.")
                break
    finally:
        if owns_connection:
            conn.close()

    logger.info(
        "[OUTREACH SEND] Sent: %d | Failed: %d | Skipped: %d | Dry run: %s",
        run.sent, run.failed, run.skipped, run.dry_run,
    )
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Send approved outreach emails. Dry run by default — pass --live to "
            "actually transmit. Never approves anything and never bypasses the quality gate."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Actually send. Without this, every check runs but nothing is transmitted. "
            "Refused while OUTREACH_DRY_RUN is true — that switch must be turned off in "
            ".env first."
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap sends this run.")
    parser.add_argument("--db", default=None, help="Database path (defaults to DATABASE_PATH).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # OUTREACH_DRY_RUN is an absolute veto, not a default that a flag can
    # talk its way past. Sending is irreversible and outward-facing, so the
    # decision to enable it has to be made deliberately, in the environment,
    # rather than in the heat of typing a command.
    if args.live and settings.outreach_dry_run:
        print(
            "Refusing to send: OUTREACH_DRY_RUN is true.\n\n"
            "This is the environment safety switch, and --live does not override it.\n"
            "Sending real email to real companies is irreversible, so enabling it has to\n"
            "be a deliberate change you make once, not a flag on a single command.\n\n"
            "To send for real, set this in your .env file first:\n\n"
            "    OUTREACH_DRY_RUN=false\n\n"
            "Then run this command again with --live. Until then, run it without --live\n"
            "to see exactly what would be sent."
        )
        return 1

    dry_run = not args.live

    try:
        run = send_approved(db_path=args.db, dry_run=dry_run, limit=args.limit)
    except ValueError as exc:
        print(f"{exc}")
        return 1

    print(
        f"\nSent:    {run.sent}{'  (dry run — nothing transmitted)' if run.dry_run else ''}\n"
        f"Failed:  {run.failed}\n"
        f"Skipped: {run.skipped}\n"
        f"Daily limit: {run.daily_limit} ({run.already_sent_today} already sent today, "
        f"budget this run: {run.budget})\n"
    )
    for result in run.results:
        detail = result.error or result.skipped_reason
        state = "SENT" if result.sent else ("FAILED" if result.error else "SKIPPED")
        print(f"  {state:<8} {result.company} <{result.recipient}>" + (f" — {detail}" if detail else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
