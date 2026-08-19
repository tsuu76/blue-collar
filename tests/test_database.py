"""
Tests for the SQLite layer: schema creation, job insertion, deduplication,
and status transitions. Uses a throwaway in-memory-like temp file DB per
test — never touches data/jobs.db.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.database.db import get_connection, init_db
from src.database.jobs_repo import (
    DuplicateJobError,
    find_by_dedupe_hash,
    get_job,
    insert_job,
    list_jobs_by_status,
    update_job_analysis,
    update_job_status,
)
from src.database.models import JobStatus, canonicalize_url, compute_dedupe_hash


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test_jobs.db"
    init_db(path)
    return path


@pytest.fixture()
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


SAMPLE_JOB = {
    "source": "manual_paste",
    "url": "https://example.com/jobs/it-support-1",
    "title": "IT Support Officer",
    "company": "Example Pty Ltd",
    "location": "Sydney NSW",
    "description": "Entry-level IT support role on our service desk.",
}


class TestSchema:
    def test_init_db_creates_all_tables(self, db_path):
        connection = get_connection(db_path)
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        connection.close()
        expected = {
            "jobs",
            "applications",
            "job_sources",
            "resume_versions",
            "cover_letters",
            "settings",
        }
        assert expected.issubset(tables)

    def test_init_db_is_idempotent(self, db_path):
        # Calling init_db again on an existing DB must not raise.
        init_db(db_path)
        init_db(db_path)


class TestDedupeHash:
    def test_same_title_company_location_same_hash(self):
        h1 = compute_dedupe_hash("IT Support Officer", "Acme", "Sydney", "https://a.com/1")
        h2 = compute_dedupe_hash("  it support officer  ", "ACME", "SYDNEY", "https://a.com/2")
        assert h1 == h2

    def test_different_company_different_hash(self):
        h1 = compute_dedupe_hash("IT Support Officer", "Acme", "Sydney", "https://a.com/1")
        h2 = compute_dedupe_hash("IT Support Officer", "Other Co", "Sydney", "https://a.com/1")
        assert h1 != h2

    def test_falls_back_to_url_when_no_company(self):
        h1 = compute_dedupe_hash("", "", "", "https://a.com/job/1")
        h2 = compute_dedupe_hash("", "", "", "https://a.com/job/1")
        h3 = compute_dedupe_hash("", "", "", "https://a.com/job/2")
        assert h1 == h2
        assert h1 != h3

    def test_source_and_source_job_id_takes_priority(self):
        # Same platform id must dedupe even if title/company text differs
        # slightly (e.g. a re-titled posting) — the platform id is more
        # precise than fuzzy text matching.
        h1 = compute_dedupe_hash(
            "IT Support Officer", "Acme", "Sydney", "https://boards.greenhouse.io/acme/jobs/123",
            source="greenhouse", source_job_id="123",
        )
        h2 = compute_dedupe_hash(
            "IT Support Officer (Updated)", "Acme Pty Ltd", "Sydney NSW", "https://boards.greenhouse.io/acme/jobs/123?utm_source=x",
            source="greenhouse", source_job_id="123",
        )
        assert h1 == h2

    def test_different_source_job_id_different_hash(self):
        h1 = compute_dedupe_hash("Title", "Acme", "Sydney", "https://a.com/1", source="greenhouse", source_job_id="123")
        h2 = compute_dedupe_hash("Title", "Acme", "Sydney", "https://a.com/1", source="greenhouse", source_job_id="456")
        assert h1 != h2

    def test_same_source_job_id_different_source_different_hash(self):
        h1 = compute_dedupe_hash("Title", "Acme", "Sydney", "https://a.com/1", source="greenhouse", source_job_id="123")
        h2 = compute_dedupe_hash("Title", "Acme", "Sydney", "https://a.com/1", source="lever", source_job_id="123")
        assert h1 != h2

    def test_manual_import_unaffected_by_new_params(self):
        # Existing manual-import call sites never pass source/source_job_id
        # — confirms the default empty-string behavior is unchanged.
        h1 = compute_dedupe_hash("IT Support Officer", "Acme", "Sydney", "https://a.com/1")
        h2 = compute_dedupe_hash("IT Support Officer", "Acme", "Sydney", "https://a.com/1", source="", source_job_id="")
        assert h1 == h2


class TestCanonicalizeUrl:
    def test_strips_tracking_params(self):
        assert canonicalize_url("https://a.com/job/1?utm_source=x&utm_campaign=y") == canonicalize_url("https://a.com/job/1")

    def test_strips_trailing_slash(self):
        assert canonicalize_url("https://a.com/job/1/") == canonicalize_url("https://a.com/job/1")

    def test_lowercases_host(self):
        assert canonicalize_url("https://Example.COM/job/1") == canonicalize_url("https://example.com/job/1")

    def test_strips_fragment(self):
        assert canonicalize_url("https://a.com/job/1#apply") == canonicalize_url("https://a.com/job/1")

    def test_keeps_non_tracking_query_params(self):
        assert canonicalize_url("https://a.com/job?id=1") != canonicalize_url("https://a.com/job?id=2")

    def test_empty_url_returns_empty(self):
        assert canonicalize_url("") == ""

    def test_malformed_url_does_not_raise(self):
        canonicalize_url("not a url :::")  # must not raise


class TestInsertJob:
    def test_insert_and_get(self, conn):
        job_id = insert_job(conn, SAMPLE_JOB)
        conn.commit()
        row = get_job(conn, job_id)
        assert row["title"] == "IT Support Officer"
        assert row["status"] == "NEW"

    def test_duplicate_raises(self, conn):
        insert_job(conn, SAMPLE_JOB)
        conn.commit()
        with pytest.raises(DuplicateJobError):
            insert_job(conn, SAMPLE_JOB)

    def test_duplicate_error_carries_existing_id(self, conn):
        job_id = insert_job(conn, SAMPLE_JOB)
        conn.commit()
        try:
            insert_job(conn, SAMPLE_JOB)
            assert False, "expected DuplicateJobError"
        except DuplicateJobError as exc:
            assert exc.existing_job_id == job_id

    def test_same_title_different_company_not_duplicate(self, conn):
        job_id_1 = insert_job(conn, SAMPLE_JOB)
        other = dict(SAMPLE_JOB, company="Different Company", url="https://example.com/jobs/it-support-2")
        job_id_2 = insert_job(conn, other)
        conn.commit()
        assert job_id_1 != job_id_2

    def test_stores_canonical_url(self, conn):
        job = dict(SAMPLE_JOB, url="https://Example.com/jobs/it-support-1/?utm_source=x")
        job_id = insert_job(conn, job)
        conn.commit()
        row = get_job(conn, job_id)
        assert row["canonical_url"] == "https://example.com/jobs/it-support-1"

    def test_stores_discovery_metadata(self, conn):
        job = dict(SAMPLE_JOB, discovery_metadata={"posted_date": "2026-08-01", "application_url": "https://example.com/apply/1"})
        job_id = insert_job(conn, job)
        conn.commit()
        row = get_job(conn, job_id)
        assert row["discovery_metadata_json"] is not None
        import json

        assert json.loads(row["discovery_metadata_json"])["posted_date"] == "2026-08-01"

    def test_no_discovery_metadata_is_null(self, conn):
        job_id = insert_job(conn, SAMPLE_JOB)
        conn.commit()
        row = get_job(conn, job_id)
        assert row["discovery_metadata_json"] is None

    def test_same_source_job_id_is_duplicate_even_with_different_title(self, conn):
        job = dict(SAMPLE_JOB, source="greenhouse", source_job_id="123", url="https://boards.greenhouse.io/acme/jobs/123")
        insert_job(conn, job)
        conn.commit()
        retitled = dict(
            SAMPLE_JOB,
            title="IT Support Officer (Updated)",
            source="greenhouse",
            source_job_id="123",
            url="https://boards.greenhouse.io/acme/jobs/123?utm_source=x",
        )
        with pytest.raises(DuplicateJobError):
            insert_job(conn, retitled)


class TestStatusTransitions:
    def test_update_job_status(self, conn):
        job_id = insert_job(conn, SAMPLE_JOB)
        conn.commit()
        update_job_status(conn, job_id, JobStatus.QUALIFIED)
        conn.commit()
        row = get_job(conn, job_id)
        assert row["status"] == JobStatus.QUALIFIED

    def test_update_job_status_with_rejection_reason(self, conn):
        job_id = insert_job(conn, SAMPLE_JOB)
        conn.commit()
        update_job_status(conn, job_id, JobStatus.REJECTED, rejection_reason="Senior role")
        conn.commit()
        row = get_job(conn, job_id)
        assert row["status"] == JobStatus.REJECTED
        assert row["rejection_reason"] == "Senior role"

    def test_list_jobs_by_status(self, conn):
        id1 = insert_job(conn, SAMPLE_JOB)
        other = dict(SAMPLE_JOB, company="Other Co", url="https://example.com/jobs/it-support-2")
        id2 = insert_job(conn, other)
        conn.commit()
        update_job_status(conn, id2, JobStatus.QUALIFIED)
        conn.commit()

        new_jobs = list_jobs_by_status(conn, JobStatus.NEW)
        qualified_jobs = list_jobs_by_status(conn, JobStatus.QUALIFIED)
        assert {row["id"] for row in new_jobs} == {id1}
        assert {row["id"] for row in qualified_jobs} == {id2}


class TestUpdateJobAnalysis:
    def test_update_job_analysis_sets_fields(self, conn):
        job_id = insert_job(conn, SAMPLE_JOB)
        conn.commit()
        update_job_analysis(
            conn,
            job_id,
            category="ENTRY_LEVEL_IT",
            fit_score=88,
            experience_required="0-1 years",
            ai_analysis_json='{"fit_score": 88}',
        )
        conn.commit()
        row = get_job(conn, job_id)
        assert row["category"] == "ENTRY_LEVEL_IT"
        assert row["fit_score"] == 88
        assert row["experience_required"] == "0-1 years"
