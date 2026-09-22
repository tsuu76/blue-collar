"""
Tests for the Phase 14 Flask dashboard, using Flask's test client (no real
HTTP server, no browser). A temp SQLite db is used throughout — this suite
never touches data/jobs.db.
"""
from __future__ import annotations

import json
from unittest.mock import patch

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
        # The pipeline board (status-columns view) moved from `/` to
        # `/board` when `/` became the client-facing landing page — see
        # src/dashboard/app.py:board_page and the "client-facing UI"
        # commit that introduced home.html. The board's behaviour is
        # unchanged, only its URL moved, so the assertion still tests
        # what it did before, at the URL where that view now lives.
        insert_sample_job(db_path)
        resp = client.get("/board")
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

        # See note above — the pipeline board is at /board now.
        resp = client.get("/board")
        assert b"IT Support Officer" in resp.data


class TestManualApplicationCards:
    """
    The READY_TO_APPLY column's CardSwap widget (src/dashboard/static/
    cardswap-react.js, built from frontend/cardswap/) reads its job data
    from a <script type="application/json"> island the server renders —
    these tests cover that server-side data, not the React/animation side
    (which is verified manually in-browser; see the CardSwap integration
    notes). Real jobs.fit_score / ai_analysis_json columns only — no new
    schema.
    """

    def _make_ready_job(self, db_path, *, title, fit_score, reason=None, application_url=None, **overrides):
        from src.database.jobs_repo import update_job_analysis, update_job_status

        job_id = insert_sample_job(
            db_path,
            title=title,
            status=JobStatus.READY_TO_APPLY,
            discovery_metadata=({"application_url": application_url} if application_url else None),
            **overrides,
        )
        conn = get_connection(db_path)
        update_job_status(conn, job_id, JobStatus.READY_TO_APPLY)
        analysis = {"reasons": [reason]} if reason else {"reasons": []}
        update_job_analysis(conn, job_id, category="ENTRY_LEVEL_IT", fit_score=fit_score, experience_required="0-1 years", ai_analysis_json=json.dumps(analysis))
        conn.commit()
        conn.close()
        return job_id

    def test_data_island_includes_fit_score_and_reason(self, client, db_path):
        # The CardSwap widget lives on the pipeline board, which moved
        # from `/` to `/board` when the client-facing landing page took
        # over `/`. The data island is rendered by the same template,
        # so the test still exercises exactly what it did before.
        self._make_ready_job(db_path, title="IT Support Officer", fit_score=82, reason="Strong entry-level match")

        resp = client.get("/board")
        html = resp.data.decode()
        start = html.index('id="cardswap-jobs-data"')
        payload = json.loads(html[html.index(">", start) + 1 : html.index("</script>", start)])

        assert len(payload) == 1
        card = payload[0]
        assert card["title"] == "IT Support Officer"
        assert card["fit_score"] == 82
        assert card["reason"] == "Strong entry-level match"

    def test_data_island_sorted_by_fit_score_descending(self, client, db_path):
        self._make_ready_job(db_path, title="Lower Match", fit_score=40, url="https://example.com/jobs/low")
        self._make_ready_job(db_path, title="Higher Match", fit_score=90, url="https://example.com/jobs/high")

        resp = client.get("/board")
        html = resp.data.decode()
        start = html.index('id="cardswap-jobs-data"')
        payload = json.loads(html[html.index(">", start) + 1 : html.index("</script>", start)])

        assert [c["title"] for c in payload] == ["Higher Match", "Lower Match"]

    def test_data_island_href_prefers_application_url(self, client, db_path):
        self._make_ready_job(
            db_path,
            title="Service Desk Analyst",
            fit_score=70,
            application_url="https://boards.greenhouse.io/acme/jobs/1/apply",
            url="https://boards.greenhouse.io/acme/jobs/1",
        )

        resp = client.get("/board")
        html = resp.data.decode()
        start = html.index('id="cardswap-jobs-data"')
        payload = json.loads(html[html.index(">", start) + 1 : html.index("</script>", start)])

        assert payload[0]["href"] == "https://boards.greenhouse.io/acme/jobs/1/apply"

    def test_no_ready_to_apply_jobs_omits_cardswap_mount(self, client):
        # The CardSwap widget is on the pipeline board (moved from `/`
        # to `/board`) — asserting the mount is ABSENT still needs to
        # run against that page, not the landing dashboard.
        resp = client.get("/board")
        assert b'id="cardswap-root"' not in resp.data
        assert b'id="cardswap-jobs-data"' not in resp.data


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


class TestCheckLink:
    def test_check_link_reachable(self, client, db_path):
        job_id = insert_sample_job(db_path, url="https://example.com/jobs/1")
        with patch("src.dashboard.app.check_url_reachable") as mock_check:
            from src.browser_assist.reachability import ReachabilityResult

            mock_check.return_value = ReachabilityResult(reachable=True, status_code=200)
            resp = client.post(f"/job/{job_id}/check-link", follow_redirects=True)
        assert resp.status_code == 200
        assert b"reachable" in resp.data

    def test_check_link_dead(self, client, db_path):
        job_id = insert_sample_job(db_path, url="https://example.com/jobs/expired")
        with patch("src.dashboard.app.check_url_reachable") as mock_check:
            from src.browser_assist.reachability import ReachabilityResult

            mock_check.return_value = ReachabilityResult(reachable=False, status_code=404)
            resp = client.post(f"/job/{job_id}/check-link", follow_redirects=True)
        assert resp.status_code == 200
        assert b"dead or expired" in resp.data

    def test_job_detail_shows_detected_platform(self, client, db_path):
        job_id = insert_sample_job(db_path, url="https://boards.greenhouse.io/acme/jobs/123")
        resp = client.get(f"/job/{job_id}")
        assert b"Greenhouse" in resp.data


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
