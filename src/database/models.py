"""
Shared constants and small value objects for the database layer.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit


class JobStatus:
    NEW = "NEW"
    ANALYZING = "ANALYZING"
    QUALIFIED = "QUALIFIED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    SKIPPED = "SKIPPED"

    ALL = {NEW, ANALYZING, QUALIFIED, READY_TO_APPLY, APPLIED, REJECTED, INTERVIEW, OFFER, SKIPPED}


class ApplicationType:
    TYPE_A = "TYPE_A"  # assisted, accessible employer ATS/form
    TYPE_B = "TYPE_B"  # manual / external — goes to the manual queue


class OutreachStatus:
    """
    Lifecycle of one direct-outreach message (see outreach_messages in
    schema.sql). Separate vocabulary from JobStatus: a job moves toward
    "apply to this posting", a message moves toward "this was sent".

    DRAFT -> APPROVED -> SENT is the only path to a real email. FAILED is a
    send that was attempted and errored (retryable). REJECTED is a draft the
    user read and turned down; it keeps the history but stops blocking a
    future redraft. DO_NOT_CONTACT is a draft permanently blocked because the
    company opted out — it is never retried and never counts as a send.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SENT = "SENT"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"

    ALL = {DRAFT, APPROVED, SENT, FAILED, REJECTED, DO_NOT_CONTACT}

    # Statuses a message can still legitimately leave — anything else is a
    # settled outcome the sending step must not touch.
    SENDABLE = {APPROVED, FAILED}


_WHITESPACE_RE = re.compile(r"\s+")

# Common tracking/analytics query params that don't change what job a URL
# points to — stripped so "the same posting with a different ?utm_source="
# doesn't get treated as a different job.
_TRACKING_PARAM_PREFIXES = ("utm_", "gh_src", "gh_jid", "trk", "ref", "source", "fbclid", "gclid")


def normalize_for_dedupe(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text or "").strip().lower()


def canonicalize_url(url: str) -> str:
    """
    Normalize a URL for comparison: lowercase scheme+host, drop the
    fragment, drop common tracking query params, drop a trailing slash.
    Never raises — an unparseable URL is returned normalized-for-dedupe
    as-is rather than crashing the caller.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return normalize_for_dedupe(url)

    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or ""

    if parts.query:
        kept = [
            kv
            for kv in parts.query.split("&")
            if kv and not kv.split("=", 1)[0].lower().startswith(_TRACKING_PARAM_PREFIXES)
        ]
        query = "&".join(sorted(kept))
    else:
        query = ""

    return urlunsplit((scheme, netloc, path, query, ""))


def compute_dedupe_hash(
    title: str, company: str, location: str, url: str, *, source: str = "", source_job_id: str = ""
) -> str:
    """
    Deduplication key, in priority order:
      1. (source + source_job_id), when both are given — the most precise
         signal available: a platform-assigned id (e.g. a Greenhouse job
         id) can't collide across genuinely different postings the way
         title/company text sometimes can.
      2. (title + company + location) — so the same job posted to two
         different sources (or re-posted) is still recognized as a
         duplicate.
      3. canonicalized URL alone — for a bare pasted link with no
         description yet.
    """
    if source and source_job_id:
        basis = f"source_id|{normalize_for_dedupe(source)}|{normalize_for_dedupe(source_job_id)}"
    elif company and title:
        basis = f"{normalize_for_dedupe(title)}|{normalize_for_dedupe(company)}|{normalize_for_dedupe(location)}"
    else:
        basis = canonicalize_url(url)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def domain_of(url_or_email: str) -> str:
    """
    Best-effort host for a URL or email address, lowercased with a leading
    "www." stripped. Never raises — an unparseable value returns "" so the
    caller falls back to name-based matching rather than crashing.
    """
    value = (url_or_email or "").strip().lower()
    if not value:
        return ""
    if "@" in value:
        return value.rsplit("@", 1)[1].strip().strip("/")
    if "//" not in value:
        value = f"https://{value}"
    try:
        netloc = urlsplit(value).netloc
    except ValueError:
        return ""
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def compute_company_dedupe_hash(name: str, website: str = "") -> str:
    """
    Deduplication key for an outreach company, in priority order:
      1. the website's domain — the same company is routinely written
         "SafetyCulture", "Safety Culture" and "SafetyCulture Pty Ltd", but
         only ever has one domain.
      2. the normalized name — so a target with no website is still
         deduplicated rather than becoming contactable twice.
    """
    domain = domain_of(website)
    basis = f"domain|{domain}" if domain else f"name|{normalize_for_dedupe(name)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
