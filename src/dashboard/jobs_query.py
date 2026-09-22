"""
Read-only query helpers for the client-facing Jobs page.

Every query in here answers a question the UI actually asks — "which jobs
match this text and these filters?", "what values does this filter offer
today?" — and does so from the same `jobs` table every other read already
uses. Nothing here is caching or duplicating the write path; nothing here
mutates a row.

The filter surface is deliberately small: only fields the existing schema
reliably populates (title, company, description, location, source, status,
date_found) are exposed as searchable or filterable. Fields the schema does
not carry (employment type, remote/hybrid flag) are NOT exposed as filters
here — pretending they were would fail the "do not invent data" rule.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from src.config import settings


# Job statuses shown in the Jobs page status filter. Deliberately excludes
# statuses a normal user has no reason to filter by (ANALYZING is a
# transient pipeline state); every value here is drawn from the same
# JobStatus vocabulary jobs_repo uses.
JOBS_STATUS_FILTER = [
    "NEW",
    "QUALIFIED",
    "READY_TO_APPLY",
    "APPLIED",
    "INTERVIEW",
    "OFFER",
    "SKIPPED",
    "REJECTED",
]


# Whitelist of sort keys the UI is allowed to request. A user-supplied
# `sort` value is looked up here before it is spliced into SQL, so no
# untrusted string ever reaches the ORDER BY clause.
_SORT_CLAUSES: dict[str, str] = {
    "recent": "date_found DESC, id DESC",
    "oldest": "date_found ASC, id ASC",
    "match": "fit_score DESC NULLS LAST, date_found DESC",
    "title": "LOWER(title) ASC, id ASC",
    "company": "LOWER(company) ASC, LOWER(title) ASC",
}
DEFAULT_SORT = "recent"


def _target_locations() -> tuple[str, ...]:
    """
    Snapshot of the current `settings.target_locations` as an immutable
    tuple. Called at query-time so tests that monkeypatch settings pick
    up the current value, but the JobsQuery dataclass stays frozen.
    """
    return tuple(settings.target_locations)


def _target_location_clause(field_name: str, targets: tuple[str, ...]) -> tuple[str, list[str]]:
    """
    Build a SQL fragment that matches the same set of jobs
    `src.job_filter.location.location_matches` would call a match:
    case-insensitive substring against ANY of the target locations
    (Sydney, Greater Sydney, NSW, Remote Australia, Hybrid Sydney by
    default). Returns (clause, params).

    Deliberately mirrors location_matches so the UI's default view and
    the filter/scoring stage never disagree on what "in Sydney" means.
    """
    if not targets:
        return "1=1", []
    parts = [f"LOWER({field_name}) LIKE LOWER(?)" for _ in targets]
    params = [f"%{target}%" for target in targets]
    # Empty/NULL location can't match — location_matches("", ...)
    # returns False, so the SQL fragment must too.
    return "(" + f"{field_name} IS NOT NULL AND TRIM({field_name}) != '' AND (" + " OR ".join(parts) + "))", params


@dataclass(frozen=True)
class JobsQuery:
    """
    A parsed, validated request for the Jobs page. Never contains raw
    user text bound into SQL — the LIKE patterns are built here, the
    ORDER BY key is looked up here.

    `scope` controls the target-region default. "target" (the default)
    only returns jobs whose location matches settings.target_locations
    — the same match rule src.job_filter.location.location_matches
    applies — which is the app's Sydney/NSW/Remote AU focus. "anywhere"
    widens the view; the user opts into it explicitly via the filter
    bar. An explicit `location` value narrows further within either
    scope.
    """

    q: str = ""
    location: str = ""
    source: str = ""
    status: str = ""
    sort: str = DEFAULT_SORT
    limit: int = 200
    scope: str = "target"

    def order_by_clause(self) -> str:
        return _SORT_CLAUSES.get(self.sort, _SORT_CLAUSES[DEFAULT_SORT])


def parse_jobs_query(args) -> JobsQuery:
    """
    Turn a Flask `request.args`-like mapping into a JobsQuery. Every
    field is coerced to a string and trimmed; unknown sort keys fall
    back to the default rather than raise, so a bookmarked URL never
    breaks the page.
    """
    def _get(key: str, default: str = "") -> str:
        raw = args.get(key, default) if args is not None else default
        return (raw or "").strip()

    limit_raw = _get("limit", "200")
    try:
        limit = max(1, min(500, int(limit_raw)))
    except ValueError:
        limit = 200

    sort = _get("sort", DEFAULT_SORT)
    if sort not in _SORT_CLAUSES:
        sort = DEFAULT_SORT

    # `scope=anywhere` widens the default target-region filter. Any
    # other value (or absent) keeps the app's Sydney/NSW/Remote AU
    # default — Blue Collar is a target-region tool.
    scope = "anywhere" if _get("scope").lower() == "anywhere" else "target"

    return JobsQuery(
        q=_get("q"),
        location=_get("location"),
        source=_get("source"),
        status=_get("status"),
        sort=sort,
        limit=limit,
        scope=scope,
    )


def search_jobs(conn: sqlite3.Connection, query: JobsQuery) -> list[sqlite3.Row]:
    """
    Return job rows matching `query`. Text search is a case-insensitive
    LIKE across title/company/description/location (the fields the
    schema actually stores). Filters use exact equality on `location`,
    `source`, and `status` — a user picks these from a dropdown of real
    values, so exact match is the honest thing to do.

    Unless `query.scope == "anywhere"`, results are constrained to
    `settings.target_locations` — the same match rule
    `src.job_filter.location.location_matches` uses. An explicit
    `location` value still applies within either scope, so the target
    default never blocks a legitimate user filter.
    """
    where: list[str] = []
    params: list[object] = []

    if query.scope != "anywhere":
        clause, clause_params = _target_location_clause("location", _target_locations())
        where.append(clause)
        params.extend(clause_params)

    if query.q:
        pattern = f"%{query.q}%"
        # LOWER on both sides — SQLite's LIKE is case-insensitive for
        # ASCII by default, but jobs contain accented characters from
        # some employers ("Zürich", "São Paulo") that only match after
        # explicit lower-casing.
        where.append(
            "("
            "LOWER(title) LIKE LOWER(?) OR "
            "LOWER(company) LIKE LOWER(?) OR "
            "LOWER(description) LIKE LOWER(?) OR "
            "LOWER(location) LIKE LOWER(?)"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern])

    if query.location:
        where.append("location = ?")
        params.append(query.location)

    if query.source:
        where.append("source = ?")
        params.append(query.source)

    if query.status:
        where.append("status = ?")
        params.append(query.status)

    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {query.order_by_clause()} LIMIT ?"
    params.append(query.limit)

    return conn.execute(sql, params).fetchall()


def distinct_locations(
    conn: sqlite3.Connection, *, limit: int = 200, target_only: bool = True
) -> list[str]:
    """
    Every non-empty location currently stored on any job, sorted
    alphabetically. Used to populate the Location filter dropdown so a
    user only ever picks a value that actually appears in the data.

    `target_only` (default) restricts the dropdown to locations that
    match settings.target_locations — the dropdown must not offer
    Toronto or Berlin as filter options in a Sydney-focused tool. When
    the user has explicitly widened the scope to "anywhere" the
    dropdown widens too.
    """
    if target_only:
        clause, clause_params = _target_location_clause("location", _target_locations())
        sql = (
            "SELECT DISTINCT location FROM jobs "
            f"WHERE location IS NOT NULL AND TRIM(location) != '' AND {clause} "
            "ORDER BY LOWER(location) ASC LIMIT ?"
        )
        params: list[object] = list(clause_params) + [limit]
    else:
        sql = (
            "SELECT DISTINCT location FROM jobs "
            "WHERE location IS NOT NULL AND TRIM(location) != '' "
            "ORDER BY LOWER(location) ASC LIMIT ?"
        )
        params = [limit]
    rows = conn.execute(sql, params).fetchall()
    return [row["location"] for row in rows]


def distinct_sources(conn: sqlite3.Connection) -> list[str]:
    """
    Every distinct value of the `source` column in the jobs table, in
    order of how many jobs share it (most-common-first). This is the
    raw source token; the sources_view module turns it into a friendly
    label for display.
    """
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM jobs "
        "WHERE source IS NOT NULL AND TRIM(source) != '' "
        "GROUP BY source ORDER BY n DESC, source ASC"
    ).fetchall()
    return [row["source"] for row in rows]


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Map of status -> job count. Statuses with zero jobs are omitted,
    which the caller handles by defaulting to zero. Used by the landing
    page's overview tiles — every number shown there is one of these.
    """
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


def total_jobs(conn: sqlite3.Connection, *, target_only: bool = True) -> int:
    """
    How many jobs the app has stored. Defaults to counting only
    target-region jobs — the client-facing dashboard is Sydney-focused
    and a "Total jobs: 2003" figure that included Toronto and Berlin
    would misrepresent the tool. `target_only=False` is used by the
    pipeline board where the reviewer wants the raw pipeline total.
    """
    if target_only:
        clause, params = _target_location_clause("location", _target_locations())
        row = conn.execute(f"SELECT COUNT(*) AS n FROM jobs WHERE {clause}", params).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    return int(row["n"]) if row else 0


def last_job_found_at(conn: sqlite3.Connection, *, target_only: bool = True) -> str | None:
    """
    Most recent `date_found`, or None if no rows qualify. When
    `target_only` is on, a run that only turned up overseas jobs
    doesn't move this timestamp — which matches the client-facing
    story that the app's job is finding Sydney-relevant openings.
    """
    if target_only:
        clause, params = _target_location_clause("location", _target_locations())
        row = conn.execute(f"SELECT MAX(date_found) AS m FROM jobs WHERE {clause}", params).fetchone()
    else:
        row = conn.execute("SELECT MAX(date_found) AS m FROM jobs").fetchone()
    if row is None:
        return None
    return row["m"]


def recent_jobs(
    conn: sqlite3.Connection, *, limit: int = 10, target_only: bool = True
) -> list[sqlite3.Row]:
    """
    The N most recently discovered jobs, newest first. Defaults to
    target-region only — the client-facing "Recent jobs" strip must
    not surface a Toronto AI Engineer role in a Sydney-focused search.
    """
    if target_only:
        clause, params = _target_location_clause("location", _target_locations())
        sql = (
            f"SELECT * FROM jobs WHERE {clause} "
            "ORDER BY date_found DESC, id DESC LIMIT ?"
        )
        return conn.execute(sql, params + [limit]).fetchall()
    return conn.execute(
        "SELECT * FROM jobs ORDER BY date_found DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def count_since(
    conn: sqlite3.Connection, iso_datetime: str, *, target_only: bool = True
) -> int:
    """
    How many jobs have a `date_found` strictly greater than the given
    ISO timestamp. Defaults to target-region only so the "New today"
    tile answers the tool's real question, not "how many rows landed
    anywhere on earth".
    """
    if target_only:
        clause, clause_params = _target_location_clause("location", _target_locations())
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs WHERE date_found > ? AND {clause}",
            [iso_datetime] + clause_params,
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE date_found > ?",
            (iso_datetime,),
        ).fetchone()
    return int(row["n"]) if row else 0
