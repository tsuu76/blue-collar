"""
CRUD + query helpers for the `jobs` table. Kept separate from db.py so the
schema/connection module stays small and this can grow with the pipeline
phases (filtering, scoring, etc.) without turning into one giant file.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .models import compute_dedupe_hash


class DuplicateJobError(Exception):
    """Raised when a job with the same dedupe hash already exists."""

    def __init__(self, existing_job_id: int):
        self.existing_job_id = existing_job_id
        super().__init__(f"Duplicate job (existing id={existing_job_id})")


def find_by_dedupe_hash(conn: sqlite3.Connection, dedupe_hash: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM jobs WHERE dedupe_hash = ?", (dedupe_hash,))
    return cur.fetchone()


def insert_job(conn: sqlite3.Connection, job: dict[str, Any]) -> int:
    """
    Insert a normalized job dict. Raises DuplicateJobError instead of a raw
    IntegrityError so callers (the pipeline's dedupe stage) can handle it
    explicitly and log a clean "Duplicates: N" count per the spec's
    observability requirements.

    Required keys: source, url, title, description.
    Optional: source_job_id, company, location, salary, category,
    experience_required, fit_score, status, application_type.
    """
    dedupe_hash = compute_dedupe_hash(
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location", ""),
        url=job.get("url", ""),
    )
    existing = find_by_dedupe_hash(conn, dedupe_hash)
    if existing:
        raise DuplicateJobError(existing["id"])

    cur = conn.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, url, title, company, location, salary,
            description, category, experience_required, fit_score, status,
            dedupe_hash, application_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.get("source"),
            job.get("source_job_id"),
            job.get("url"),
            job.get("title"),
            job.get("company"),
            job.get("location"),
            job.get("salary"),
            job.get("description"),
            job.get("category"),
            job.get("experience_required"),
            job.get("fit_score"),
            job.get("status", "NEW"),
            dedupe_hash,
            job.get("application_type"),
        ),
    )
    return cur.lastrowid


def update_job_status(conn: sqlite3.Connection, job_id: int, status: str, rejection_reason: str | None = None) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, rejection_reason = COALESCE(?, rejection_reason), updated_at = datetime('now')
        WHERE id = ?
        """,
        (status, rejection_reason, job_id),
    )


def update_job_analysis(conn: sqlite3.Connection, job_id: int, category: str, fit_score: int, experience_required: str, ai_analysis_json: str) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET category = ?, fit_score = ?, experience_required = ?, ai_analysis_json = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (category, fit_score, experience_required, ai_analysis_json, job_id),
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return cur.fetchone()


def list_jobs_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY fit_score DESC NULLS LAST, date_found DESC", (status,))
    return cur.fetchall()


def list_all_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM jobs ORDER BY date_found DESC")
    return cur.fetchall()
