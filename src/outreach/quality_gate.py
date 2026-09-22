"""
The outreach quality gate — the last deterministic check before a message can
become eligible for sending.

**No AI runs here.** Every check is code comparing stored text against stored
text, so the verdict is reproducible: the same message and the same evidence
always produce the same result, and nothing can be talked into passing.

It deliberately re-verifies things earlier stages already checked. The drafting
stage validated the email as it was written, and the pipeline skipped companies
that didn't qualify — but a draft can be edited afterwards, a company can opt
out afterwards, and a bug in either stage would otherwise go unnoticed. This
gate assumes nothing upstream did its job, and re-derives every answer from
the database and the master resume.

Enforcement is not advisory. `outreach_repo.approve_message` only promotes a
message whose `gate_passed` is 1, and `list_approved_messages` re-checks the
same column, so a FAIL here cannot become sendable by any route — including a
caller that never runs this module, since an ungated message has
`gate_passed = NULL` and is refused exactly like a failure.

Run it with `evaluate_message(conn, message_id, resume)`; persist the verdict
with `gate_and_record(...)`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from src.config import settings
from src.database import outreach_repo
from src.database.models import OutreachStatus
from src.resume.fabrication_guard import (
    find_fabricated_numbers,
    find_suspicious_terms,
    skill_words,
)
from src.resume.schema import MasterResume

from .email_draft import (
    APPLICATION_PHRASES,
    OUTREACH_CLICHES,
    PLACEHOLDER_RE,
    SUBJECT_MAX_WORDS,
    as_sentences,
    profile_links,
)

logger = logging.getLogger("job_hunter.outreach.quality_gate")


class Check:
    """Stable names for every gate check, so failures are machine-readable."""

    NOT_DO_NOT_CONTACT = "company_not_do_not_contact"
    NOT_ALREADY_CONTACTED = "company_not_already_contacted"
    NO_DUPLICATE_PENDING = "no_duplicate_pending_message"
    REAL_POSTINGS = "real_postings_found"
    VERIFIED_CONTACT_EMAIL = "verified_contact_email"
    VERIFIED_OVERLAP = "verified_resume_overlap"
    COMPANY_SKILLS_REAL = "company_skills_in_real_postings"
    CANDIDATE_SKILLS_REAL = "candidate_skills_in_resume"
    NO_FABRICATION = "no_fabricated_information"
    NO_BANNED_PHRASES = "no_banned_or_cliche_phrases"
    WORD_COUNT = "within_word_count_limits"
    DAILY_LIMIT = "daily_outreach_limit_not_exceeded"

    ALL = [
        NOT_DO_NOT_CONTACT, NOT_ALREADY_CONTACTED, NO_DUPLICATE_PENDING, REAL_POSTINGS,
        VERIFIED_CONTACT_EMAIL, VERIFIED_OVERLAP, COMPANY_SKILLS_REAL, CANDIDATE_SKILLS_REAL,
        NO_FABRICATION, NO_BANNED_PHRASES, WORD_COUNT, DAILY_LIMIT,
    ]


PASS = "PASS"
FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {"check": self.name, "passed": self.passed, "reason": self.reason or None}


@dataclass
class GateResult:
    """
    Structured verdict. `verdict` is the literal string PASS or FAIL;
    `reasons` lists one plain-language reason per failed check.
    """

    message_id: int | None = None
    company: str = ""
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks) and bool(self.checks)

    @property
    def verdict(self) -> str:
        return PASS if self.passed else FAIL

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    @property
    def reasons(self) -> list[str]:
        return [f"{check.name}: {check.reason}" for check in self.failures]

    def add(self, name: str, passed: bool, reason: str = "") -> None:
        self.checks.append(CheckResult(name, passed, "" if passed else reason))

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "company": self.company,
            "verdict": self.verdict,
            "passed": self.passed,
            "reasons": self.reasons,
            "checks": [check.to_dict() for check in self.checks],
        }


def _load_json(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _evidence_texts(snapshot: dict) -> list[str]:
    """Every piece of the company's own posting text available to us."""
    texts = [snapshot.get("company") or ""]
    for posting in snapshot.get("postings") or []:
        texts.extend(
            [posting.get("title") or "", posting.get("description") or "", posting.get("location") or ""]
        )
    return [text for text in texts if text]


def _vocabulary(texts: list[str]) -> set[str]:
    words: set[str] = set()
    for text in texts:
        words |= {w for w in skill_words({text.lower()}) if w}
    return words


def _term_is_grounded(term: str, vocabulary: set[str]) -> bool:
    """A term counts as grounded when every meaningful word of it appears."""
    words = [w for w in skill_words({term.lower()}) if len(w) > 1]
    return bool(words) and all(word in vocabulary for word in words)


def evaluate_message(
    conn: sqlite3.Connection, message_id: int, resume: MasterResume
) -> GateResult:
    """
    Run every gate check against one stored message and return a structured
    PASS/FAIL. Reads only from the database and the master resume — no
    network, no AI, no side effects.

    A message that does not exist raises ValueError; everything else that
    could go wrong is a FAIL with a reason, never an exception.
    """
    message = outreach_repo.get_message(conn, message_id)
    if message is None:
        raise ValueError(f"No outreach message with id {message_id}")

    company = outreach_repo.get_company(conn, message["company_id"])
    result = GateResult(message_id=message_id, company=company["name"] if company else "")

    if company is None:
        result.add(Check.NOT_DO_NOT_CONTACT, False, "the company record for this message is missing")
        return result

    snapshot = _load_json(message["research_snapshot_json"], {})
    analysis_blob = _load_json(message["analysis_json"], {})
    analysis = analysis_blob.get("analysis", {}) if isinstance(analysis_blob, dict) else {}

    subject = message["subject"] or ""
    body = message["body"] or ""
    combined = f"{subject}\n{body}"
    lowered = combined.lower()

    # --- 1. Company must not have opted out -------------------------------
    result.add(
        Check.NOT_DO_NOT_CONTACT,
        not company["do_not_contact"],
        company["do_not_contact_reason"] or "this company is marked do-not-contact",
    )

    # --- 2. Company must not already have been emailed --------------------
    result.add(
        Check.NOT_ALREADY_CONTACTED,
        not outreach_repo.has_been_contacted(conn, company["id"]),
        "this company has already been contacted",
    )

    # --- 3. No other draft/approved message for the same company ----------
    result.add(
        Check.NO_DUPLICATE_PENDING,
        not outreach_repo.has_other_pending_message(conn, company["id"], message_id),
        "another message to this company is already drafted or approved",
    )

    # --- 4. Real postings must have been found ----------------------------
    postings = snapshot.get("postings") or []
    result.add(
        Check.REAL_POSTINGS,
        bool(postings),
        "no real job postings were recorded for this message — nothing grounds it",
    )

    # --- 5. A verified contact address must exist -------------------------
    recipient = (message["recipient_email"] or "").strip()
    contact = (company["contact_email"] or "").strip()
    if not recipient:
        result.add(Check.VERIFIED_CONTACT_EMAIL, False, "this message has no recipient address")
    elif not contact:
        result.add(
            Check.VERIFIED_CONTACT_EMAIL, False, "the company has no verified contact address"
        )
    elif recipient.lower() != contact.lower():
        # The address on the message must still be the company's verified
        # one — otherwise an edit could redirect the email elsewhere.
        result.add(
            Check.VERIFIED_CONTACT_EMAIL,
            False,
            f"recipient {recipient!r} is not this company's verified address {contact!r}",
        )
    else:
        result.add(Check.VERIFIED_CONTACT_EMAIL, True)

    # --- 6. Verified overlap with the real resume -------------------------
    overlap = [str(item) for item in (analysis.get("candidate_overlap") or [])]
    result.add(
        Check.VERIFIED_OVERLAP,
        bool(overlap),
        "no verified overlap between this company's postings and the resume",
    )

    # --- 7. Company-side skills must appear in the real postings ----------
    evidence_texts = _evidence_texts(snapshot)
    evidence_vocab = _vocabulary(evidence_texts)
    claimed_company_terms = [
        str(term)
        for key in ("recurring_skills", "tools_and_technologies", "terminology")
        for term in (analysis.get(key) or [])
    ]
    ungrounded = [
        term for term in claimed_company_terms if not _term_is_grounded(term, evidence_vocab)
    ]
    result.add(
        Check.COMPANY_SKILLS_REAL,
        not ungrounded,
        f"claimed about this company but absent from their postings: {sorted(set(ungrounded))}",
    )

    # --- 8. Candidate-side skills must exist in the real resume -----------
    resume_skills = resume.all_skills()
    resume_vocab = set(resume_skills) | skill_words(resume_skills)
    not_mine = [term for term in overlap if not _term_is_grounded(term, resume_vocab)]
    result.add(
        Check.CANDIDATE_SKILLS_REAL,
        not not_mine,
        f"claimed as the candidate's but absent from the resume: {sorted(set(not_mine))}",
    )

    # --- 9. Nothing in the prose invented about company, role or candidate -
    allowed_texts = (
        resume.all_text_fragments()
        + resume.skills
        + resume.identifying_names()
        + profile_links(resume)
        + overlap
        + evidence_texts
    )
    # Proper-noun detection runs on the BODY only. Subject lines are
    # conventionally title-cased ("Entry-Level Opportunities at Xero"), which
    # makes every ordinary word look like a proper noun to a guard built for
    # prose — "Opportunities" was flagged as a fabricated claim on a
    # perfectly honest draft. Factual claims live in the body; a subject is a
    # label. Numbers, clichés and placeholders below still cover both.
    guard_text = as_sentences(body)
    fabrication_problems: list[str] = []

    fabricated_numbers = find_fabricated_numbers(as_sentences(combined), allowed_texts)
    if fabricated_numbers:
        fabrication_problems.append(
            f"number(s) traceable to neither the resume nor the postings: {sorted(fabricated_numbers)}"
        )

    suspicious = find_suspicious_terms(guard_text, allowed_texts, resume_skills)
    if suspicious:
        fabrication_problems.append(
            f"term(s) found in neither the resume nor the postings: {sorted(suspicious)}"
        )

    placeholders = PLACEHOLDER_RE.findall(combined)
    if placeholders:
        fabrication_problems.append(f"unfilled template placeholder(s): {placeholders}")

    result.add(Check.NO_FABRICATION, not fabrication_problems, "; ".join(fabrication_problems))

    # --- 10. Banned/cliché phrasing ---------------------------------------
    phrase_problems: list[str] = []
    cliches = [phrase for phrase in OUTREACH_CLICHES if phrase in lowered]
    if cliches:
        phrase_problems.append(f"cliché phrasing: {sorted(cliches)}")
    # Claiming to apply for an advertised role is a factual error in general
    # outreach, so it belongs with the banned phrasing rather than passing.
    applying = [phrase for phrase in APPLICATION_PHRASES if phrase in lowered]
    if applying:
        phrase_problems.append(f"claims to be applying for an advertised job: {sorted(applying)}")
    result.add(Check.NO_BANNED_PHRASES, not phrase_problems, "; ".join(phrase_problems))

    # --- 11. Word count ---------------------------------------------------
    count = len(body.split())
    lo, hi = settings.outreach_email_min_words, settings.outreach_email_max_words
    if not body.strip():
        result.add(Check.WORD_COUNT, False, "the email body is empty")
    elif count < lo:
        result.add(Check.WORD_COUNT, False, f"too short: {count} words (minimum {lo})")
    elif count > hi:
        result.add(Check.WORD_COUNT, False, f"too long: {count} words (maximum {hi})")
    elif not subject.strip():
        result.add(Check.WORD_COUNT, False, "the email has no subject line")
    elif len(subject.split()) > SUBJECT_MAX_WORDS:
        result.add(
            Check.WORD_COUNT,
            False,
            f"subject is too long: {len(subject.split())} words (maximum {SUBJECT_MAX_WORDS})",
        )
    else:
        result.add(Check.WORD_COUNT, True)

    # --- 12. Daily outreach limit -----------------------------------------
    sent_today = outreach_repo.count_sent_today(conn)
    result.add(
        Check.DAILY_LIMIT,
        sent_today < settings.outreach_daily_limit,
        f"daily outreach limit reached ({sent_today}/{settings.outreach_daily_limit} sent today)",
    )

    return result


def gate_and_record(
    conn: sqlite3.Connection, message_id: int, resume: MasterResume
) -> GateResult:
    """
    Evaluate a message and persist the verdict on it.

    This is what makes the message approvable (or keeps it from ever being
    approved). The caller commits.
    """
    result = evaluate_message(conn, message_id, resume)
    outreach_repo.save_gate_result(
        conn, message_id, passed=result.passed, reasons=result.reasons
    )
    if result.passed:
        logger.info("Quality gate PASS for message %d (%s).", message_id, result.company)
    else:
        logger.warning(
            "Quality gate FAIL for message %d (%s): %s",
            message_id, result.company, "; ".join(result.reasons),
        )
    return result


def is_eligible(conn: sqlite3.Connection, message_id: int) -> bool:
    """
    Whether a stored message currently satisfies the gate's recorded verdict.
    A message that has never been gated is not eligible — absence of a PASS
    is treated as a FAIL, never as permission.
    """
    message = outreach_repo.get_message(conn, message_id)
    if message is None:
        return False
    return (
        message["gate_passed"] == 1
        and message["status"] in {OutreachStatus.DRAFT, OutreachStatus.APPROVED}
    )
