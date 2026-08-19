"""
Discovery orchestrator — the actual integration point between discovery and
the rest of the system:

    n8n scheduler -> Job Discovery -> Normalize -> Deduplicate ->
    EXISTING job-import pipeline -> EXISTING filtering/scoring/tailoring/
    cover-letter/QC/PDF -> EXISTING dashboard + notifications

run_discovery() is deliberately thin: for every enabled employer, fetch via
its adapter (already-normalized NormalizedJob objects — normalization
happens inside each adapter, not here), insert into the jobs table via the
EXISTING src.database.jobs_repo.insert_job() (which already handles
dedup), then hand off to the EXISTING
src.pipeline.process_job.process_new_jobs() — exactly the same function a
manually-imported job already goes through. Nothing here duplicates
filtering, scoring, tailoring, cover-letter generation, QC, or PDF
rendering; those all still happen exactly where they already did.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.database.db import get_connection
from src.database.jobs_repo import DuplicateJobError, insert_job
from src.pipeline.process_job import process_new_jobs

from .registry import discover_from_employer, load_employers

logger = logging.getLogger("job_hunter.job_discovery.run")


def run_discovery(*, db_path: str | None = None, process: bool = True, config_path: Path | None = None) -> dict:
    """
    Run discovery across every enabled employer, insert newly-found jobs,
    then (unless process=False) run them through the existing pipeline.

    Returns a summary dict with discovery counts and, if process=True, the
    pipeline's own counts nested under "pipeline" — the same dict shape
    process_new_jobs() already returns, unchanged.

    config_path overrides the employer registry location (used by tests to
    point at a temp config instead of the real config/employers.json).
    """
    employers = [e for e in load_employers(config_path) if e.enabled]
    found = 0
    inserted = 0
    duplicates = 0

    conn = get_connection(db_path)
    try:
        for employer in employers:
            jobs = discover_from_employer(employer)
            found += len(jobs)
            for job in jobs:
                try:
                    insert_job(conn, job.to_dict())
                    inserted += 1
                except DuplicateJobError:
                    duplicates += 1
            conn.commit()
    finally:
        conn.close()

    logger.info(
        "[JOB DISCOVERY] Employers checked: %d | Found: %d | Inserted: %d | Duplicates: %d",
        len(employers),
        found,
        inserted,
        duplicates,
    )

    result = {
        "employers_checked": len(employers),
        "found": found,
        "inserted": inserted,
        "duplicates": duplicates,
    }

    if process:
        result["pipeline"] = process_new_jobs(db_path=db_path)

    return result
