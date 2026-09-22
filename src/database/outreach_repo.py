"""
CRUD + query helpers for the direct-outreach tables (outreach_companies,
outreach_messages).

Same shape as jobs_repo.py / applications_repo.py: plain functions taking an
open sqlite3.Connection as the first argument, returning sqlite3.Row objects,
with commit/close left to the caller. No ORM, no connection ownership here.

The status transitions are deliberately guarded rather than being blind
UPDATEs (see approve_message / mark_message_sent). This is the storage layer
of a review queue whose whole purpose is that nothing reaches a real
employer's inbox without being approved first — so "you can only send what
was approved" is enforced here, in the one place every future caller has to
go through, instead of being re-litigated in each of them.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .models import OutreachStatus, compute_company_dedupe_hash


class DuplicateCompanyError(Exception):
    """
    Raised when a company with the same dedupe hash is already an outreach
    target. Mirrors jobs_repo.DuplicateJobError so callers can count
    duplicates cleanly instead of catching a raw IntegrityError — and so the
    same company can never be queued, and later emailed, twice.
    """

    def __init__(self, existing_company_id: int):
        self.existing_company_id = existing_company_id
        super().__init__(f"Duplicate outreach company (existing id={existing_company_id})")


# --------------------------------------------------------------------------
# Companies
# --------------------------------------------------------------------------

def find_company_by_dedupe_hash(conn: sqlite3.Connection, dedupe_hash: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM outreach_companies WHERE dedupe_hash = ?", (dedupe_hash,))
    return cur.fetchone()


def insert_company(conn: sqlite3.Connection, company: dict[str, Any]) -> int:
    """
    Insert an outreach company.

    Required key: name. Optional: website, contact_email, source, notes,
    platform, identifier. Raises DuplicateCompanyError if this company is
    already stored, and ValueError if the name is blank.
    """
    name = (company.get("name") or "").strip()
    if not name:
        raise ValueError("outreach company requires a name")

    website = (company.get("website") or "").strip()
    dedupe_hash = compute_company_dedupe_hash(name, website)
    existing = find_company_by_dedupe_hash(conn, dedupe_hash)
    if existing:
        raise DuplicateCompanyError(existing["id"])

    contact_email = (company.get("contact_email") or "").strip().lower()
    cur = conn.execute(
        """
        INSERT INTO outreach_companies (
            name, website, contact_email, source, notes, platform, identifier, dedupe_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            website or None,
            contact_email or None,
            company.get("source", "config"),
            company.get("notes"),
            (company.get("platform") or "").strip().lower() or None,
            (company.get("identifier") or "").strip() or None,
            dedupe_hash,
        ),
    )
    return cur.lastrowid


def upsert_company(conn: sqlite3.Connection, company: dict[str, Any]) -> int:
    """
    Get-or-create by dedupe hash, returning the company id either way.

    This is what a re-run of any future discovery/research step should use:
    re-processing the same target list must be idempotent rather than
    raising. Deliberately does NOT overwrite an existing row's fields — a
    contact address you corrected by hand must survive the next run.
    """
    name = (company.get("name") or "").strip()
    if not name:
        raise ValueError("outreach company requires a name")
    dedupe_hash = compute_company_dedupe_hash(name, (company.get("website") or "").strip())
    existing = find_company_by_dedupe_hash(conn, dedupe_hash)
    if existing:
        return existing["id"]
    return insert_company(conn, company)


def get_company(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM outreach_companies WHERE id = ?", (company_id,))
    return cur.fetchone()


def list_companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM outreach_companies ORDER BY created_at DESC, id DESC")
    return cur.fetchall()


def list_contactable_companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Companies that have a contact address and have not opted out — the only
    ones any future drafting/sending step may act on.
    """
    cur = conn.execute(
        """
        SELECT * FROM outreach_companies
        WHERE do_not_contact = 0 AND contact_email IS NOT NULL AND TRIM(contact_email) != ''
        ORDER BY created_at ASC, id ASC
        """
    )
    return cur.fetchall()


def update_company_contact_email(conn: sqlite3.Connection, company_id: int, contact_email: str) -> None:
    """Set or correct the address outreach to this company goes to."""
    normalized = (contact_email or "").strip().lower()
    conn.execute(
        """
        UPDATE outreach_companies
        SET contact_email = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (normalized or None, company_id),
    )


def save_discovered_contact(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    email: str = "",
    name: str = "",
    source: str = "",
    evidence_url: str = "",
    linkedin_url: str = "",
    x_handle: str = "",
    x_name: str = "",
    all_addresses: list[dict] | None = None,
) -> None:
    """
    Record what src/outreach/contact_discovery.py found for a company.

    Two deliberate asymmetries, both in the SQL so no caller can forget them:

      - A discovered address NEVER overwrites one that is already stored.
        The stored address was put there by the user (config, or the
        dashboard form), and a page-scrape must not be able to redirect
        their outreach to a different mailbox.
      - A blank discovery never clears what is already known. A company
        whose site was down this run keeps the provenance it had.

    `all_addresses` is the full inventory of everything the search
    published-only-found on the company's own pages and in real ATS
    postings — each item {email, source, evidence_url}. It's stored on
    discovered_contacts_json so the dashboard can offer a picker; the
    address the sender uses is still `contact_email`. Passing None (the
    default) or an empty list preserves whatever was previously stored
    in that column — the same "never clear what's known" rule the
    single-address fields follow. Duplicates are collapsed by
    lower-cased email (see _merge_discovered_addresses).

    linkedin_url is stored for the user to act on by hand. Nothing in this
    codebase sends a LinkedIn message, and this column is never read by the
    sending path.
    """
    merged_json: str | None = None
    if all_addresses:
        existing = _read_discovered_contacts(conn, company_id)
        merged = _merge_discovered_addresses(existing, all_addresses)
        merged_json = json.dumps(merged) if merged else None

    conn.execute(
        """
        UPDATE outreach_companies
        SET contact_email = CASE
                WHEN contact_email IS NULL OR TRIM(contact_email) = ''
                THEN COALESCE(NULLIF(?, ''), contact_email)
                ELSE contact_email
            END,
            contact_name         = COALESCE(NULLIF(?, ''), contact_name),
            contact_source       = COALESCE(NULLIF(?, ''), contact_source),
            contact_evidence_url = COALESCE(NULLIF(?, ''), contact_evidence_url),
            linkedin_url         = COALESCE(NULLIF(?, ''), linkedin_url),
            x_handle             = COALESCE(NULLIF(?, ''), x_handle),
            x_name               = COALESCE(NULLIF(?, ''), x_name),
            discovered_contacts_json = COALESCE(?, discovered_contacts_json),
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            (email or "").strip().lower(),
            (name or "").strip(),
            (source or "").strip(),
            (evidence_url or "").strip(),
            (linkedin_url or "").strip(),
            (x_handle or "").strip().lower(),
            (x_name or "").strip(),
            merged_json,
            company_id,
        ),
    )


def _read_discovered_contacts(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    row = conn.execute(
        "SELECT discovered_contacts_json FROM outreach_companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if row is None or not row["discovered_contacts_json"]:
        return []
    try:
        parsed = json.loads(row["discovered_contacts_json"])
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _merge_discovered_addresses(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """
    Union `existing` and `incoming` on lowercased email, preserving the
    earliest entry's `source`/`evidence_url` (existing wins ties, so a
    later re-run can't rewrite the provenance of an address the user
    has already seen on the dashboard). Non-dict or emailless entries
    are silently skipped — the whole point of this column is verifiable
    addresses.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for source_list in (existing, incoming):
        if not isinstance(source_list, list):
            continue
        for item in source_list:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            merged.append({
                "email": email,
                "source": str(item.get("source") or "").strip(),
                "evidence_url": str(item.get("evidence_url") or "").strip(),
            })
    return merged


def get_discovered_contacts(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    """
    Public accessor for the picker inventory. Returns [] for an
    unknown company or an empty column, never raises.
    """
    return _read_discovered_contacts(conn, company_id)


def update_company_ats(
    conn: sqlite3.Connection, company_id: int, platform: str, identifier: str
) -> None:
    """
    Record which public ATS board this company's postings come from, so the
    research layer doesn't have to re-resolve it from config/employers.json
    on every run.
    """
    conn.execute(
        """
        UPDATE outreach_companies
        SET platform = ?, identifier = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        ((platform or "").strip().lower() or None, (identifier or "").strip() or None, company_id),
    )


def save_company_research(conn: sqlite3.Connection, company_id: int, research: dict) -> None:
    """
    Store a summary of the last research run so the dashboard can show what
    was actually found without re-fetching anyone's job board on page load.
    """
    conn.execute(
        """
        UPDATE outreach_companies
        SET research_json = ?, researched_at = datetime('now'), updated_at = datetime('now')
        WHERE id = ?
        """,
        (json.dumps(research), company_id),
    )


def set_do_not_contact(conn: sqlite3.Connection, company_id: int, reason: str = "") -> int:
    """
    Mark a company as never-contact and block its unsent messages.

    Returns the number of messages blocked. Blocking the existing queue is
    the point: flagging the company but leaving an APPROVED draft sitting in
    the send queue would let the opt-out be honoured everywhere except the
    one place it actually matters. SENT rows are left alone — they're
    history, not intent.
    """
    conn.execute(
        """
        UPDATE outreach_companies
        SET do_not_contact = 1, do_not_contact_reason = COALESCE(?, do_not_contact_reason),
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (reason or None, company_id),
    )
    cur = conn.execute(
        """
        UPDATE outreach_messages
        SET status = ?, updated_at = datetime('now')
        WHERE company_id = ? AND status IN (?, ?, ?)
        """,
        (
            OutreachStatus.DO_NOT_CONTACT,
            company_id,
            OutreachStatus.DRAFT,
            OutreachStatus.APPROVED,
            OutreachStatus.FAILED,
        ),
    )
    return cur.rowcount


def is_do_not_contact(conn: sqlite3.Connection, company_id: int) -> bool:
    row = conn.execute(
        "SELECT do_not_contact FROM outreach_companies WHERE id = ?", (company_id,)
    ).fetchone()
    return bool(row["do_not_contact"]) if row else False


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def insert_message(conn: sqlite3.Connection, message: dict[str, Any]) -> int:
    """
    Insert a message. Required key: company_id. Optional: recipient_email,
    subject, body, status.

    Messages are created as DRAFT by default. A caller may pass an explicit
    status, but there is no path here that creates something already
    APPROVED by accident — approval is its own function with its own guard.
    """
    company_id = message["company_id"]
    recipient = (message.get("recipient_email") or "").strip().lower()
    analysis = message.get("analysis")
    snapshot = message.get("research_snapshot")
    cur = conn.execute(
        """
        INSERT INTO outreach_messages (
            company_id, recipient_email, subject, body, analysis_json,
            research_snapshot_json, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            recipient or None,
            message.get("subject"),
            message.get("body"),
            json.dumps(analysis) if analysis is not None else None,
            json.dumps(snapshot) if snapshot is not None else None,
            message.get("status", OutreachStatus.DRAFT),
        ),
    )
    return cur.lastrowid


def get_message(conn: sqlite3.Connection, message_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM outreach_messages WHERE id = ?", (message_id,))
    return cur.fetchone()


def list_messages_by_status(
    conn: sqlite3.Connection, status: str, limit: int | None = None
) -> list[sqlite3.Row]:
    """Oldest first — a review/send queue should drain in the order it filled."""
    sql = "SELECT * FROM outreach_messages WHERE status = ? ORDER BY created_at ASC, id ASC"
    params: list[Any] = [status]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_messages_for_company(conn: sqlite3.Connection, company_id: int) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM outreach_messages WHERE company_id = ? ORDER BY created_at ASC, id ASC",
        (company_id,),
    )
    return cur.fetchall()


def update_message_content(
    conn: sqlite3.Connection, message_id: int, *, subject: str | None = None, body: str | None = None
) -> bool:
    """
    Edit a draft's subject/body. Only DRAFT messages are editable: silently
    rewriting text that was already approved would make the approval
    meaningless. Returns True if the message was updated.

    Editing clears any previous gate verdict. The gate checked the old text;
    keeping its PASS would let edited wording inherit approval it was never
    granted, so the message must be re-gated before it can be approved.
    """
    cur = conn.execute(
        """
        UPDATE outreach_messages
        SET subject = COALESCE(?, subject), body = COALESCE(?, body),
            gate_passed = NULL, gate_reasons_json = NULL, updated_at = datetime('now')
        WHERE id = ? AND status = ?
        """,
        (subject, body, message_id, OutreachStatus.DRAFT),
    )
    return cur.rowcount > 0


def save_gate_result(
    conn: sqlite3.Connection, message_id: int, *, passed: bool, reasons: list[str]
) -> None:
    """
    Record the deterministic quality gate's verdict for one message. This is
    the only thing that can make a message approvable — see approve_message.
    """
    conn.execute(
        """
        UPDATE outreach_messages
        SET gate_passed = ?, gate_reasons_json = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (1 if passed else 0, json.dumps(reasons), message_id),
    )


def approve_message(conn: sqlite3.Connection, message_id: int) -> bool:
    """
    Promote DRAFT -> APPROVED, stamping approved_at.

    Two conditions, both in the WHERE clause so they cannot be bypassed by a
    caller that forgets them:

      - status must be DRAFT, so approving an already-sent or opted-out
        message is a no-op rather than quietly re-arming it.
      - gate_passed must be 1. A message that has not passed the
        deterministic quality gate (src/outreach/quality_gate.py), or that
        failed it, can never become eligible for sending. A NULL here means
        "never gated", which is treated exactly like a failure.

    Returns True if the message was approved.
    """
    cur = conn.execute(
        """
        UPDATE outreach_messages
        SET status = ?, approved_at = datetime('now'), updated_at = datetime('now')
        WHERE id = ? AND status = ? AND gate_passed = 1
        """,
        (OutreachStatus.APPROVED, message_id, OutreachStatus.DRAFT),
    )
    return cur.rowcount > 0


def reject_message(conn: sqlite3.Connection, message_id: int) -> bool:
    """
    Turn down a draft the user has read. Only a DRAFT or APPROVED message can
    be rejected — a sent email cannot be un-sent, so rejecting one is a no-op
    returning False rather than a status rewrite that would misrepresent
    history. Returns True if the message was rejected.
    """
    cur = conn.execute(
        """
        UPDATE outreach_messages
        SET status = ?, updated_at = datetime('now')
        WHERE id = ? AND status IN (?, ?)
        """,
        (OutreachStatus.REJECTED, message_id, OutreachStatus.DRAFT, OutreachStatus.APPROVED),
    )
    return cur.rowcount > 0


def approve_messages(conn: sqlite3.Connection, message_ids: list[int]) -> list[int]:
    """
    Approve several drafts in one action, for the review UI's bulk control.

    Each one goes through approve_message, so the same DRAFT-only guard
    applies individually — an id that is already sent, rejected or blocked is
    skipped rather than failing the whole batch. Returns the ids actually
    approved.
    """
    return [message_id for message_id in message_ids if approve_message(conn, message_id)]


def list_approved_messages(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """
    The send queue: APPROVED messages whose company has not since opted out.

    The join matters — a company can be flagged do-not-contact between
    approval and sending, and the send step must see the current answer, not
    the one that was true when the draft was written.

    gate_passed is re-checked here as well as in approve_message. Belt and
    braces: this is the query the send step drains, so it must not depend on
    the approval path having been the only way in.

    Four independent conditions, all required:
      - status APPROVED       — a DRAFT is never sendable
      - gate_passed = 1       — the quality gate cleared it (NULL is a refusal)
      - do_not_contact = 0    — checked live, not as of approval time
      - sent_at IS NULL       — never re-send something already sent
    """
    sql = """
        SELECT m.* FROM outreach_messages m
        JOIN outreach_companies c ON c.id = m.company_id
        WHERE m.status = ?
          AND m.gate_passed = 1
          AND c.do_not_contact = 0
          AND m.sent_at IS NULL
        ORDER BY m.created_at ASC, m.id ASC
    """
    params: list[Any] = [OutreachStatus.APPROVED]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def record_send_attempt(conn: sqlite3.Connection, message_id: int) -> bool:
    """
    Note that a delivery attempt is about to be made: increment
    send_attempts, leaving status APPROVED and sent_at NULL.

    Called immediately BEFORE handing the message to SMTP, and committed, so
    that a crash mid-delivery leaves visible evidence that an attempt was in
    flight. It deliberately does not mark anything as sent — only SMTP
    returning successfully does that.
    """
    cur = conn.execute(
        """
        UPDATE outreach_messages
        SET send_attempts = send_attempts + 1, updated_at = datetime('now')
        WHERE id = ? AND sent_at IS NULL
        """,
        (message_id,),
    )
    return cur.rowcount > 0


def mark_message_sent(conn: sqlite3.Connection, message_id: int) -> bool:
    """
    Record a successful send: status SENT, sent_at stamped, previous error
    cleared.

    Two guards in the WHERE clause:
      - status must be sendable (APPROVED, or FAILED being retried), so an
        unapproved draft cannot be marked sent even by a buggy caller.
      - sent_at must still be NULL, so a message can only ever be marked
        sent once. This is the last line of defence against a double send
        being recorded if the sender is run twice concurrently.

    send_attempts is NOT incremented here — record_send_attempt already did
    that before the SMTP call, so counting it again would double-count.
    Returns True if the message was updated.
    """
    cur = conn.execute(
        f"""
        UPDATE outreach_messages
        SET status = ?, sent_at = datetime('now'), last_error = NULL,
            updated_at = datetime('now')
        WHERE id = ? AND sent_at IS NULL
          AND status IN ({",".join("?" * len(OutreachStatus.SENDABLE))})
        """,
        (OutreachStatus.SENT, message_id, *sorted(OutreachStatus.SENDABLE)),
    )
    return cur.rowcount > 0


def mark_message_failed(conn: sqlite3.Connection, message_id: int, error: str) -> bool:
    """
    Record a failed send attempt: status FAILED, error stored.

    The message is kept in full — subject, body, evidence, gate verdict — so
    nothing is lost to a transient mail-server problem. It leaves the send
    queue (which only drains APPROVED), so a broken address can't be retried
    in a loop; requeue_failed_message puts it back deliberately.

    send_attempts is not incremented here: record_send_attempt counted this
    attempt before the SMTP call was made.
    """
    cur = conn.execute(
        f"""
        UPDATE outreach_messages
        SET status = ?, last_error = ?, updated_at = datetime('now')
        WHERE id = ? AND sent_at IS NULL
          AND status IN ({",".join("?" * len(OutreachStatus.SENDABLE))})
        """,
        (OutreachStatus.FAILED, error, message_id, *sorted(OutreachStatus.SENDABLE)),
    )
    return cur.rowcount > 0


def requeue_failed_message(conn: sqlite3.Connection, message_id: int) -> bool:
    """
    Put a FAILED message back in the send queue after you've fixed whatever
    broke — a wrong SMTP password, an outage, a bounced address.

    Deliberately manual and deliberately narrow: only a FAILED message that
    still carries a quality-gate PASS and has never been sent can be
    requeued, so this can neither resurrect a rejected draft nor bypass the
    gate nor cause a second delivery.
    """
    cur = conn.execute(
        """
        UPDATE outreach_messages
        SET status = ?, updated_at = datetime('now')
        WHERE id = ? AND status = ? AND gate_passed = 1 AND sent_at IS NULL
        """,
        (OutreachStatus.APPROVED, message_id, OutreachStatus.FAILED),
    )
    return cur.rowcount > 0


def count_sent_today(conn: sqlite3.Connection) -> int:
    """
    Messages actually sent since local midnight — the number the daily
    sending limit is enforced against. Counts by sent_at rather than by
    draft creation, so a large drafting run can never consume the day's
    quota before a single email goes out.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM outreach_messages
        WHERE sent_at IS NOT NULL AND date(sent_at, 'localtime') = date('now', 'localtime')
        """
    ).fetchone()
    return row["n"] if row else 0


def has_pending_message(conn: sqlite3.Connection, company_id: int) -> bool:
    """
    True if this company already has a message waiting (DRAFT or APPROVED).
    Stops a second draft piling up behind one that hasn't gone out yet.
    """
    row = conn.execute(
        "SELECT 1 FROM outreach_messages WHERE company_id = ? AND status IN (?, ?) LIMIT 1",
        (company_id, OutreachStatus.DRAFT, OutreachStatus.APPROVED),
    ).fetchone()
    return row is not None


def count_created_today(conn: sqlite3.Connection) -> int:
    """
    Messages drafted since local midnight. The generation pipeline budgets
    against this so a run can't produce hundreds of drafts in a day, and so
    running it twice in one day doesn't double the day's output.

    Counted separately from count_sent_today: drafting and sending are
    different acts with different risks, and a draft costs nothing but
    local compute.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM outreach_messages
        WHERE date(created_at, 'localtime') = date('now', 'localtime')
        """
    ).fetchone()
    return row["n"] if row else 0


def has_other_pending_message(
    conn: sqlite3.Connection, company_id: int, message_id: int
) -> bool:
    """
    True if this company has a DRAFT or APPROVED message OTHER than the one
    given. The quality gate uses this to check a message for duplicates
    without the message flagging itself.
    """
    row = conn.execute(
        """
        SELECT 1 FROM outreach_messages
        WHERE company_id = ? AND id != ? AND status IN (?, ?) LIMIT 1
        """,
        (company_id, message_id, OutreachStatus.DRAFT, OutreachStatus.APPROVED),
    ).fetchone()
    return row is not None


def has_been_contacted(conn: sqlite3.Connection, company_id: int) -> bool:
    """True if any message to this company has actually been sent."""
    row = conn.execute(
        "SELECT 1 FROM outreach_messages WHERE company_id = ? AND sent_at IS NOT NULL LIMIT 1",
        (company_id,),
    ).fetchone()
    return row is not None
