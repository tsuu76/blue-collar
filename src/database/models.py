"""
Shared constants and small value objects for the database layer.
"""
from __future__ import annotations

import hashlib
import re


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


def normalize_for_dedupe(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text or "").strip().lower()


def compute_dedupe_hash(title: str, company: str, location: str, url: str) -> str:
    """
    Deduplication key. Prefer (title + company + location) so the same job
    posted to two different sources (or re-posted) is recognized as a
    duplicate; fall back to the URL alone when company/location are missing
    (e.g. a bare pasted link before description import).
    """
    if company and title:
        basis = f"{normalize_for_dedupe(title)}|{normalize_for_dedupe(company)}|{normalize_for_dedupe(location)}"
    else:
        basis = normalize_for_dedupe(url)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
