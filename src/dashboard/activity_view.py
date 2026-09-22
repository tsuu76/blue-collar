"""
Read-only activity feed synthesised from the jobs table.

No dedicated events/audit table exists in this project's schema. To
avoid inventing one just for a page, the activity feed here groups
existing job rows by `date_found` (day + company) and emits a small,
human-readable line for each cluster: "Stripe — 9 new jobs (14 Sep)".

Every number and every timestamp on the page therefore points at real
rows in the database. If the same employer's discovery run inserted 9
jobs on the same day, that becomes one activity item. This means the
feed is not exhaustive — a run that inserted zero jobs never shows up
here, because no rows exist to derive it from — and that is worth
being honest about in the UI's own empty-state text.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .sources_view import friendly_source_label, slugify_company


@dataclass(frozen=True)
class ActivityItem:
    """One line of the activity feed."""

    date: str                # YYYY-MM-DD (from date_found)
    company: str
    source: str              # raw jobs.source, for the friendly-label logic
    count: int
    label: str               # e.g. "Stripe — 9 new jobs"
    slug: str                # empty if no employer slug can be derived


def _make_label(company: str, source: str, count: int) -> str:
    who = friendly_source_label(source, company)
    verb = "new jobs" if count != 1 else "new job"
    return f"{who} — {count} {verb}"


def build_activity(conn: sqlite3.Connection, *, limit: int = 40) -> list[ActivityItem]:
    """
    Group `jobs` by (day, company, source) and return the N most recent
    clusters. Newest-first. `limit` caps the number of ACTIVITY ITEMS
    returned, not the number of jobs; a busy day can generate several
    items (one per employer) and each is one row here.
    """
    rows = conn.execute(
        """
        SELECT
            substr(date_found, 1, 10) AS day,
            company,
            source,
            COUNT(*) AS n,
            MAX(date_found) AS last
        FROM jobs
        WHERE date_found IS NOT NULL
        GROUP BY day, company, source
        ORDER BY last DESC, company ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    items: list[ActivityItem] = []
    for row in rows:
        company = row["company"] or ""
        source = row["source"] or ""
        count = int(row["n"])
        items.append(
            ActivityItem(
                date=row["day"] or "",
                company=company,
                source=source,
                count=count,
                label=_make_label(company, source, count),
                slug=slugify_company(company) if company else "",
            )
        )
    return items
