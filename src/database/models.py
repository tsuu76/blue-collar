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
