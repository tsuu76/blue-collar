"""
Tests for the Phase 14 pipeline orchestrator: cheap filter -> AI analysis ->
scoring -> (if qualified) tailoring -> cover letter -> QC -> PDF -> DB
writes. Every AI stage is injected with a FakeAIProvider — no real Ollama
calls, no real PDF rendering slowdown avoided only where it matters (the
"full success" path does render real PDFs via the real installed Chromium,
since that's the only way to prove the whole chain actually works together).
"""
from __future__ import annotations

from src.database.db import init_db
from src.database.jobs_repo import insert_job
from src.database.models import JobStatus
from src.pipeline.process_job import process_job, process_new_jobs
from src.resume.schema import MasterResume
from tests.fakes import FakeAIProvider


def make_sample_resume() -> MasterResume:
    return MasterResume.model_validate(
        {
            "personal": {"full_name": "Test User", "email": "test@example.com"},
            "summary": "Entry-level IT candidate.",
            "skills": ["Python", "SQL", "Git"],
            "education": [
                {"id": "edu_01", "institution": "Test University", "credential": "BSc IT", "highlights": []}
            ],
            "experience": [
                {
                    "id": "exp_01",
                    "company": "Tipaload",
                    "title": "QA Tester",
                    "bullets": [{"id": "exp_01_bullet_01", "text": "Troubleshot apps in staging and production.", "skills": []}],
                }
            ],
            "projects": [],
            "certifications": [],
            "additional": [],
        }
    )


VALID_ANALYSIS_ENTRY_LEVEL = {
    "category": "ENTRY_LEVEL_IT",
    "fit_score": 88,
    "experience_required": "0-1 years",
    "desk_based": True,
    "recommendation": "APPLY",
    "matched_skills": ["Python"],
    "missing_skills": [],
    "relevant_keywords": [],
    "reasons": [],
    "concerns": [],
}

VALID_ANALYSIS_SENIOR = dict(VALID_ANALYSIS_ENTRY_LEVEL, category="MID_SENIOR_IT")

VALID_TAILORING_JSON = {
    "summary": {"action": "keep"},
    "skills": {"action": "keep"},
    "projects": {"action": "keep"},
    "experience": {"action": "keep"},
    "bullet_changes": [],
}

VALID_LETTER_TEXT = (
    "I'm applying for the IT Support Officer role at Acme. "
    "In my QA Tester role at Tipaload I troubleshot apps in staging and production environments regularly. "
) * 10


def make_test_db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


def insert_sample_job(db_path, **overrides) -> int:
    from src.database.db import get_connection

    job = {
        "source": "manual_paste",
        "url": "https://example.com/jobs/1",
        "title": "IT Support Officer",
        "company": "Acme",
        "location": "Sydney NSW",
        "description": "Entry-level role on our Sydney service desk. No experience required. Troubleshoot hardware/software issues.",
    }
    job.update(overrides)
    conn = get_connection(db_path)
    try:
        job_id = insert_job(conn, job)
        conn.commit()
        return job_id
    finally:
        conn.close()


class TestCheapFilterRejection:
    def test_senior_job_rejected_before_any_ai_call(self, tmp_path):
        db_path = make_test_db(tmp_path)
        job_id = insert_sample_job(
            db_path,
            title="Senior IT Manager",
            description="Senior IT Manager role requiring 5+ years leading a team.",
        )
        analysis_provider = FakeAIProvider(json_responses=[VALID_ANALYSIS_ENTRY_LEVEL])
        result = process_job(job_id, master_resume=make_sample_resume(), db_path=db_path, analysis_provider=analysis_provider)
        assert result.final_status == JobStatus.REJECTED
        assert analysis_provider.call_count == 0  # cheap filter short-circuited before any AI call


class TestScoringVeto:
    def test_ai_categorized_senior_is_rejected(self, tmp_path):
        db_path = make_test_db(tmp_path)
        job_id = insert_sample_job(db_path)
        analysis_provider = FakeAIProvider(json_responses=[VALID_ANALYSIS_SENIOR])
        result = process_job(job_id, master_resume=make_sample_resume(), db_path=db_path, analysis_provider=analysis_provider)
        assert result.final_status == JobStatus.REJECTED
        assert result.qualified is False


class TestAiFailureLeavesJobForRetry:
    def test_analysis_failure_leaves_status_analyzing(self, tmp_path):
        from src.ai.base import AIResponseError

        db_path = make_test_db(tmp_path)
        job_id = insert_sample_job(db_path)
        analysis_provider = FakeAIProvider(json_responses=[AIResponseError("timeout"), AIResponseError("timeout"), AIResponseError("timeout")])
        result = process_job(job_id, master_resume=make_sample_resume(), db_path=db_path, analysis_provider=analysis_provider)
        assert result.final_status == JobStatus.ANALYZING
        assert result.qualified is False


class TestTerminalStatusGuard:
    def test_applied_job_is_not_reprocessed(self, tmp_path):
        from src.database.db import get_connection
        from src.database.jobs_repo import update_job_status

        db_path = make_test_db(tmp_path)
        job_id = insert_sample_job(db_path)
        conn = get_connection(db_path)
        update_job_status(conn, job_id, JobStatus.APPLIED)
        conn.commit()
        conn.close()

        analysis_provider = FakeAIProvider(json_responses=[VALID_ANALYSIS_ENTRY_LEVEL])
        result = process_job(job_id, master_resume=make_sample_resume(), db_path=db_path, analysis_provider=analysis_provider)
        assert result.final_status == JobStatus.APPLIED
        assert analysis_provider.call_count == 0


class TestFullSuccessPath:
    """Drives the real PDF renderer (Playwright/Chromium) — slower, but the only way to prove the full chain works."""

    def test_qualified_job_produces_ready_to_apply_application(self, tmp_path):
        db_path = make_test_db(tmp_path)
        job_id = insert_sample_job(db_path)

        analysis_provider = FakeAIProvider(json_responses=[VALID_ANALYSIS_ENTRY_LEVEL])
        tailoring_provider = FakeAIProvider(json_responses=[VALID_TAILORING_JSON])
        cover_letter_provider = FakeAIProvider(text_responses=[VALID_LETTER_TEXT])
        qc_provider = FakeAIProvider(json_responses=[{"passed": True, "issues": []}])

        result = process_job(
            job_id,
            master_resume=make_sample_resume(),
            db_path=db_path,
            analysis_provider=analysis_provider,
            tailoring_provider=tailoring_provider,
            cover_letter_provider=cover_letter_provider,
            qc_provider=qc_provider,
            applications_dir=tmp_path / "applications",
        )

        assert result.final_status == JobStatus.READY_TO_APPLY
        assert result.qualified is True
        assert result.application_id is not None

        from src.database.applications_repo import get_application
        from src.database.db import get_connection

        conn = get_connection(db_path)
        try:
            app = get_application(conn, result.application_id)
            assert app["status"] == JobStatus.READY_TO_APPLY
            assert app["qc_passed"] == 1
            assert app["resume_path"] is not None

            from src.database.jobs_repo import get_job

            job_row = get_job(conn, job_id)
            assert job_row["application_type"] == "TYPE_B"
        finally:
            conn.close()

        resume_pdf = tmp_path / "applications" / "acme-it-support-officer" / "resume.pdf"
        cover_letter_pdf = tmp_path / "applications" / "acme-it-support-officer" / "cover-letter.pdf"
        assert resume_pdf.exists()
        assert cover_letter_pdf.exists()
        assert resume_pdf.read_bytes()[:4] == b"%PDF"

    def test_qc_failure_leaves_job_qualified_not_ready(self, tmp_path):
        db_path = make_test_db(tmp_path)
        job_id = insert_sample_job(db_path)

        analysis_provider = FakeAIProvider(json_responses=[VALID_ANALYSIS_ENTRY_LEVEL])
        tailoring_provider = FakeAIProvider(json_responses=[VALID_TAILORING_JSON] * 3)
        cover_letter_provider = FakeAIProvider(text_responses=[VALID_LETTER_TEXT] * 3)
        qc_provider = FakeAIProvider(json_responses=[{"passed": False, "issues": ["fabricated achievement"]}] * 3)

        result = process_job(
            job_id,
            master_resume=make_sample_resume(),
            db_path=db_path,
            analysis_provider=analysis_provider,
            tailoring_provider=tailoring_provider,
            cover_letter_provider=cover_letter_provider,
            qc_provider=qc_provider,
            applications_dir=tmp_path / "applications",
        )

        # QUALIFIED (not REJECTED, not READY_TO_APPLY) — a generated-but-
        # flagged application, per spec: "if still invalid, flag for manual
        # review" rather than silently shipping or discarding it.
        assert result.final_status == JobStatus.QUALIFIED
        assert result.qualified is True
        assert result.application_id is not None


class TestProcessNewJobsBatch:
    def test_processes_all_new_jobs_and_returns_counts(self, tmp_path):
        # process_new_jobs() doesn't currently support per-job provider
        # injection (it processes real, unpredictable batches in normal
        # use), so both jobs here are deliberately cheap-filter-rejects —
        # that keeps this test fast and fully deterministic without ever
        # reaching the AI stage. The AI-touching path is already covered by
        # TestFullSuccessPath above via process_job() directly.
        db_path = make_test_db(tmp_path)
        insert_sample_job(
            db_path,
            url="https://example.com/jobs/1",
            title="Senior IT Manager",
            company="Acme",
            description="Senior IT Manager requiring 5+ years leading a team.",
        )
        insert_sample_job(
            db_path,
            url="https://example.com/jobs/2",
            title="Principal Solutions Architect",
            company="Beta",
            description="Principal Solutions Architect, 10+ years experience required.",
        )

        counts = process_new_jobs(db_path=db_path)
        assert counts["processed"] == 2
        assert counts["rejected"] == 2
