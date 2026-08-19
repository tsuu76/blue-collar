"""
CRUD helpers for `resume_versions`, `cover_letters`, and `applications` —
the three tables written once a job clears scoring and gets a generated
resume/cover letter (see src/pipeline/process_job.py).
"""
from __future__ import annotations

import sqlite3
from typing import Any


def insert_resume_version(conn: sqlite3.Connection, job_id: int, modifications_json: str, rendered_html: str | None, pdf_path: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO resume_versions (job_id, modifications_json, rendered_html, pdf_path) VALUES (?, ?, ?, ?)",
        (job_id, modifications_json, rendered_html, pdf_path),
    )
    return cur.lastrowid


def insert_cover_letter(conn: sqlite3.Connection, job_id: int, markdown_path: str | None, pdf_path: str | None, word_count: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO cover_letters (job_id, markdown_path, pdf_path, word_count) VALUES (?, ?, ?, ?)",
        (job_id, markdown_path, pdf_path, word_count),
    )
    return cur.lastrowid


def insert_application(conn: sqlite3.Connection, application: dict[str, Any]) -> int:
    """
    Required keys: job_id, status.
    Optional: resume_version_id, cover_letter_id, resume_path, cover_letter_path,
    qc_passed (bool), qc_issues_json, notes.
    """
    qc_passed = application.get("qc_passed")
    cur = conn.execute(
        """
        INSERT INTO applications (
            job_id, resume_version_id, cover_letter_id, resume_path, cover_letter_path,
            status, qc_passed, qc_issues_json, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            application["job_id"],
            application.get("resume_version_id"),
            application.get("cover_letter_id"),
            application.get("resume_path"),
            application.get("cover_letter_path"),
            application.get("status", "NEW"),
            None if qc_passed is None else int(qc_passed),
            application.get("qc_issues_json"),
            application.get("notes"),
        ),
    )
    return cur.lastrowid


def update_application_status(conn: sqlite3.Connection, application_id: int, status: str, *, date_applied: str | None = None, notes: str | None = None) -> None:
    conn.execute(
        """
        UPDATE applications
        SET status = ?,
            date_applied = COALESCE(?, date_applied),
            notes = COALESCE(?, notes),
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (status, date_applied, notes, application_id),
    )


def get_application_by_job_id(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,))
    return cur.fetchone()


def get_application(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
    return cur.fetchone()


def list_applications_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM applications WHERE status = ? ORDER BY updated_at DESC", (status,))
    return cur.fetchall()
