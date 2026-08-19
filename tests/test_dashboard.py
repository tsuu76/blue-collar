"""
Tests for the Phase 14 Flask dashboard, using Flask's test client (no real
HTTP server, no browser). A temp SQLite db is used throughout — this suite
never touches data/jobs.db.
"""
from __future__ import annotations

import json

import pytest

from src.dashboard.app import create_app
from src.database.applications_repo import insert_application
from src.database.db import get_connection, init_db
from src.database.jobs_repo import insert_job
from src.database.models import JobStatus


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "dashboard_test.db"
    init_db(path)
    return path


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def insert_sample_job(db_path, **overrides) -> int:
    job = {
        "source": "manual_paste",
        "url": "https://example.com/jobs/1",
        "title": "IT Support Officer",
        "company": "Acme",
        "location": "Sydney NSW",
        "description": "Entry-level service desk role.",
    }
    job.update(overrides)
    conn = get_connection(db_path)
    try:
        job_id = insert_job(conn, job)
        conn.commit()
        return job_id
    finally:
        conn.close()


class TestIndex:
    def test_index_loads(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_shows_job_in_correct_column(self, client, db_path):
        insert_sample_job(db_path)
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"IT Support Officer" in resp.data
        assert b"NEW" in resp.data

    def test_index_shows_qualified_job_in_qualified_column(self, client, db_path):
        from src.database.jobs_repo import update_job_status

        job_id = insert_sample_job(db_path)
        conn = get_connection(db_path)
        update_job_status(conn, job_id, JobStatus.QUALIFIED)
        conn.commit()
        conn.close()

        resp = client.get("/")
        assert b"IT Support Officer" in resp.data


class TestJobDetail:
    def test_job_detail_loads(self, client, db_path):
        job_id = insert_sample_job(db_path)
        resp = client.get(f"/job/{job_id}")
        assert resp.status_code == 200
        assert b"IT Support Officer" in resp.data
        assert b"Acme" in resp.data

    def test_nonexistent_job_returns_404(self, client):
        resp = client.get("/job/99999")
        assert resp.status_code == 404

    def test_shows_analysis_reasons_and_skills(self, client, db_path):
        from src.database.jobs_repo import update_job_analysis

        job_id = insert_sample_job(db_path)
        conn = get_connection(db_path)
        update_job_analysis(
            conn,
            job_id,
            category="ENTRY_LEVEL_IT",
            fit_score=88,
            experience_required="0-1 years",
            ai_analysis_json=json.dumps(
                {
                    "reasons": ["Entry-level role, no experience required"],
                    "matched_skills": ["Python"],
                    "missing_skills": ["Active Directory"],
                    "concerns": [],
                }
            ),
        )
        conn.commit()
        conn.close()

        resp = client.get(f"/job/{job_id}")
        assert b"Entry-level role, no experience required" in resp.data
        assert b"Python" in resp.data
        assert b"Active Directory" in resp.data
        assert b"88" in resp.data

    def test_shows_qc_warning_when_flagged(self, client, db_path):
        job_id = insert_sample_job(db_path)
        conn = get_connection(db_path)
        insert_application(
            conn,
            {
                "job_id": job_id,
                "status": JobStatus.QUALIFIED,
                "resume_path": "/tmp/does-not-matter.pdf",
                "cover_letter_path": "/tmp/does-not-matter.pdf",
                "qc_passed": False,
                "qc_issues_json": json.dumps(["fabricated achievement"]),
            },
        )
        conn.commit()
        conn.close()

        resp = client.get(f"/job/{job_id}")
        assert b"Flagged for manual review" in resp.data
        assert b"fabricated achievement" in resp.data


class TestMarkAppliedAndSkip:
    def test_mark_applied_updates_status(self, client, db_path):
        job_id = insert_sample_job(db_path)
        resp = client.post(f"/job/{job_id}/mark-applied", follow_redirects=True)
        assert resp.status_code == 200

        conn = get_connection(db_path)
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        assert job["status"] == JobStatus.APPLIED

    def test_mark_applied_updates_application_row_too(self, client, db_path):
        job_id = insert_sample_job(db_path)
        conn = get_connection(db_path)
        insert_application(conn, {"job_id": job_id, "status": JobStatus.READY_TO_APPLY})
        conn.commit()
        conn.close()

        client.post(f"/job/{job_id}/mark-applied")

        conn = get_connection(db_path)
        app_row = conn.execute("SELECT status, date_applied FROM applications WHERE job_id=?", (job_id,)).fetchone()
        conn.close()
        assert app_row["status"] == JobStatus.APPLIED
        assert app_row["date_applied"] is not None

    def test_skip_updates_status(self, client, db_path):
        job_id = insert_sample_job(db_path)
        client.post(f"/job/{job_id}/skip")

        conn = get_connection(db_path)
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        assert job["status"] == JobStatus.SKIPPED


class TestImport:
    def test_import_form_loads(self, client):
        resp = client.get("/import")
        assert resp.status_code == 200

    def test_import_creates_new_job(self, client, db_path):
        resp = client.post(
            "/import",
            data={
                "title": "Service Desk Analyst",
                "company": "Beta Pty Ltd",
                "location": "Melbourne VIC",
                "url": "https://example.com/jobs/999",
                "description": "Entry-level service desk role, 1 year experience.",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Service Desk Analyst" in resp.data

        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE title = 'Service Desk Analyst'").fetchone()[0]
        conn.close()
        assert count == 1

    def test_import_missing_title_is_rejected(self, client):
        resp = client.post("/import", data={"title": "", "description": "Some description"})
        assert resp.status_code == 400

    def test_importing_duplicate_redirects_to_existing_job(self, client, db_path):
        job_id = insert_sample_job(db_path)
        resp = client.post(
            "/import",
            data={
                "title": "IT Support Officer",
                "company": "Acme",
                "location": "Sydney NSW",
                "description": "A different description text but same title/company/location.",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == 1  # no duplicate row created


class TestDocumentServing:
    def test_resume_pdf_404s_when_no_application(self, client, db_path):
        job_id = insert_sample_job(db_path)
        resp = client.get(f"/job/{job_id}/resume.pdf")
        assert resp.status_code == 404

    def test_resume_pdf_serves_when_file_exists(self, client, db_path, tmp_path):
        job_id = insert_sample_job(db_path)
        fake_pdf = tmp_path / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake content")

        conn = get_connection(db_path)
        insert_application(conn, {"job_id": job_id, "status": JobStatus.READY_TO_APPLY, "resume_path": str(fake_pdf)})
        conn.commit()
        conn.close()

        resp = client.get(f"/job/{job_id}/resume.pdf")
        assert resp.status_code == 200
        assert resp.data.startswith(b"%PDF")


class TestProcessRoute:
    def test_process_rejects_senior_job_without_ai_call(self, client, db_path):
        # No AI providers are faked here — this only works because a
        # cheap-filter rejection short-circuits before any AI call, so it's
        # a real, deterministic, fast test of the route wiring.
        job_id = insert_sample_job(
            db_path,
            title="Senior IT Manager",
            description="Senior IT Manager requiring 5+ years leading a team.",
        )
        resp = client.post(f"/job/{job_id}/process", follow_redirects=True)
        assert resp.status_code == 200

        conn = get_connection(db_path)
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        assert job["status"] == JobStatus.REJECTED
