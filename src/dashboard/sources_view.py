"""
Read model for the Sources page — one row per configured employer,
merged with what the database already knows about their jobs.

The employer configuration is authoritative: it decides which sources
exist and whether they're enabled. The jobs table decides what has
actually been found for each of them and when. This module joins the
two without moving the source of truth for either — nothing here writes
to `employers.json` or to `jobs`.

The `source` column on a job is a technical token ("greenhouse",
"crawl:stripe.com", "generic_careers_jsonld"). Users see a friendly
label instead: the employer's company name if we can match it, else
the platform name in title case. The mapping is deterministic and
lives here so the same friendly label is used everywhere the UI shows
one — no per-template if-ladders.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from src.job_discovery.registry import EmployerConfig, load_employers


# The `platform` values known to the discovery layer. The label shown to
# users when we can't match a job's `source` back to a configured
# employer — a manually-imported job with source="manual_paste", for
# instance — comes from here so the page never falls through to a raw
# token like "generic_careers_jsonld".
_PLATFORM_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters",
    "workday": "Workday",
    "careers_page": "Careers page",
    "crawlee_jsonld": "Careers page",  # the crawler is not the point
    "manual_paste": "Manual import",
    "manual": "Manual import",
}


@dataclass(frozen=True)
class SourceRow:
    """
    One employer as the Sources page displays it — configured
    identity, plus what the database has actually recorded for jobs
    attributed to this employer (either via company name match, or via
    the source token when the employer's platform matches uniquely).
    """

    company: str
    platform: str
    identifier: str
    enabled: bool
    job_count: int
    last_updated: str | None
    # A human-friendly one-line status. Deliberately not an exception
    # message: the UI stays calm even when discovery has never run.
    status_label: str


def slugify_company(name: str) -> str:
    """
    URL-safe slug for a company name — used to build /sources/<slug>
    without exposing raw employer strings in the URL. Deterministic and
    lossy: "Culture Amp" -> "culture-amp", "Zip Co" -> "zip-co". Not a
    stored value; recomputed everywhere it's needed.
    """
    out = []
    prev_dash = True
    for ch in (name or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def platform_label(source_token: str) -> str:
    """
    A short label naming the *kind* of source a job came from
    (Greenhouse, Lever, Careers page, Manual import), never a company
    name. Used on list rows where the company is already right next
    to it, so a per-row "via <platform>" doesn't repeat the company.
    """
    if not source_token:
        return "Unknown source"
    if source_token.startswith("crawl:"):
        return "Careers page"
    return _PLATFORM_LABELS.get(source_token, source_token.title().replace("_", " "))


def friendly_source_label(source_token: str, company: str = "") -> str:
    """
    Turn a raw `jobs.source` token into what a user should see.
    `company` (when known — e.g. from the job row itself) is preferred
    over the platform label, because "Stripe" is what the user
    recognises, not "Careers page".
    """
    if company:
        return company
    if not source_token:
        return "Unknown source"
    # "crawl:example.com" → "Example.com" (the host is a stable, honest
    # signal — every crawl:* source names one employer).
    if source_token.startswith("crawl:"):
        host = source_token[len("crawl:") :]
        return host or "Unknown source"
    return _PLATFORM_LABELS.get(source_token, source_token.title().replace("_", " "))


def _load_job_stats(
    conn: sqlite3.Connection, *, target_only: bool = True
) -> dict[str, tuple[int, str | None]]:
    """
    Group jobs by their `company` column and return {company_lower: (count,
    max_date_found)}. Lower-cased because employer entries and job rows
    aren't guaranteed to agree on capitalisation (Greenhouse returns
    exactly the string the employer put in their board).

    Defaults to target-region-only so the per-employer counts shown on
    the Sources page reflect the app's Sydney-focused view — "Airwallex
    · 609 jobs" was technically true but useless when zero of those 609
    were in Australia. `target_only=False` still available for the
    unfiltered view.
    """
    if target_only:
        from .jobs_query import _target_location_clause, _target_locations  # local to avoid cycle risk

        clause, params = _target_location_clause("location", _target_locations())
        sql = (
            "SELECT LOWER(TRIM(company)) AS c, COUNT(*) AS n, MAX(date_found) AS m "
            f"FROM jobs WHERE company IS NOT NULL AND TRIM(company) != '' AND {clause} "
            "GROUP BY c"
        )
        rows = conn.execute(sql, params).fetchall()
    else:
        rows = conn.execute(
            "SELECT LOWER(TRIM(company)) AS c, COUNT(*) AS n, MAX(date_found) AS m "
            "FROM jobs WHERE company IS NOT NULL AND TRIM(company) != '' "
            "GROUP BY c"
        ).fetchall()
    return {row["c"]: (int(row["n"]), row["m"]) for row in rows}


def _status_label(*, enabled: bool, job_count: int, last_updated: str | None) -> str:
    """
    Human-friendly one-liner. Never a stack trace, never an HTTP code —
    the raw failure detail belongs on a diagnostic page, not here.
    """
    if not enabled:
        return "Paused"
    if job_count == 0 and not last_updated:
        return "No jobs discovered yet"
    if job_count == 0:
        return "Nothing found on the last check"
    return f"{job_count} job{'s' if job_count != 1 else ''}"


def build_source_rows(
    conn: sqlite3.Connection,
    employers: Iterable[EmployerConfig] | None = None,
    *,
    target_only: bool = True,
) -> list[SourceRow]:
    """
    Build one SourceRow per configured employer. Employer rows come
    from config/employers.json (via `load_employers`); job counts and
    last-updated come from the jobs table. Employers with no jobs yet
    still show up — the Sources page has to include a configured
    employer even before the first discovery run finds anything.

    `target_only` (default) restricts per-employer counts to the
    app's target locations, so a source with 600 overseas postings
    doesn't parade as "600 jobs" on a Sydney-focused page.
    """
    employer_list = list(employers) if employers is not None else load_employers()
    job_stats = _load_job_stats(conn, target_only=target_only)

    rows: list[SourceRow] = []
    for e in employer_list:
        count, last = job_stats.get(e.company.strip().lower(), (0, None))
        rows.append(
            SourceRow(
                company=e.company,
                platform=e.platform,
                identifier=e.identifier,
                enabled=e.enabled,
                job_count=count,
                last_updated=last,
                status_label=_status_label(enabled=e.enabled, job_count=count, last_updated=last),
            )
        )

    # Active-and-recent first, then everything else alphabetically. The
    # page reads top-to-bottom, so this ordering surfaces what the user
    # is most likely to look at.
    rows.sort(
        key=lambda r: (
            not r.enabled,
            r.last_updated is None,
            r.last_updated is not None and -_iso_key(r.last_updated),
            r.company.lower(),
        )
    )
    return rows


def _iso_key(iso: str) -> int:
    """
    Sort helper: return a monotonic integer for an ISO 8601 string
    without importing datetime just for a sort. YYYY-MM-DD HH:MM:SS with
    digits removed and cast to int loses nothing that matters for
    ordering.
    """
    digits = "".join(c for c in iso if c.isdigit())
    return int(digits) if digits else 0


def find_by_slug(rows: Iterable[SourceRow], slug: str) -> SourceRow | None:
    for row in rows:
        if slugify_company(row.company) == slug:
            return row
    return None


def jobs_for_company(
    conn: sqlite3.Connection, company: str, *, limit: int = 50, target_only: bool = True
) -> list[sqlite3.Row]:
    """
    Recent job rows attributed to one company (case-insensitive match
    on the `company` column). The source-detail page uses this to show
    what that employer has recently posted, ordered newest-first.
    Defaults to target-region only, matching the rest of the client UI.
    """
    if target_only:
        from .jobs_query import _target_location_clause, _target_locations

        clause, clause_params = _target_location_clause("location", _target_locations())
        sql = (
            "SELECT * FROM jobs WHERE LOWER(TRIM(company)) = LOWER(TRIM(?)) "
            f"AND {clause} ORDER BY date_found DESC, id DESC LIMIT ?"
        )
        return conn.execute(sql, [company] + clause_params + [limit]).fetchall()
    return conn.execute(
        "SELECT * FROM jobs WHERE LOWER(TRIM(company)) = LOWER(TRIM(?)) "
        "ORDER BY date_found DESC, id DESC LIMIT ?",
        (company, limit),
    ).fetchall()
