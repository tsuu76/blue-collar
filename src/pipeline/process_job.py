"""
Pipeline orchestration — wires every earlier phase into the one flow spec
section 13 describes:

    JOB SOURCE -> NORMALIZE -> DEDUPLICATE -> CHEAP FILTER ->
    ENTRY-LEVEL IT FILTER -> LOCAL AI ANALYSIS -> FIT SCORE -> QUALIFIED? ->
    RESUME TAILORING -> COVER LETTER -> QUALITY CONTROL -> PDF GENERATION ->
    APPLICATION QUEUE

Normalize/deduplicate/source-import already happen before a job reaches the
`jobs` table (src/sources/manual_import.py + src/database/jobs_repo.py).
process_job() picks up from an existing NEW job row and runs it through the
rest: cheap filter -> AI analysis -> scoring -> (if qualified) tailoring ->
cover letter -> QC -> PDF -> DB writes.

Every stage fails safe: a rejection at any point stops the job there with a
recorded reason, and an AI failure (AIResponseError) leaves the job exactly
where it was — never advanced on unvalidated output, never crashed the
whole batch. process_new_jobs() runs this over every NEW job and logs
aggregate counts in the format spec section 30 asks for.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from src.ai.base import AIResponseError
from src.ai.job_analysis import analyze_job
from src.config import PROJECT_ROOT, settings
from src.database.applications_repo import insert_application, insert_cover_letter, insert_resume_version
from src.database.db import get_connection
from src.database.jobs_repo import get_job, update_job_analysis, update_job_status
from src.database.models import JobStatus
from src.job_filter.cheap_filter import run_cheap_filter
from src.job_filter.scoring import score_job
from src.pdf.renderer import render_cover_letter_markdown, render_cover_letter_pdf, render_resume_pdf
from src.quality_control.qc import run_quality_control_with_correction
from src.resume.schema import MasterResume
from src.resume.store import load_master_resume

logger = logging.getLogger("job_hunter.pipeline")

# Statuses a job/application can only leave via explicit user action (the
# dashboard's mark-applied/skip buttons, or a human updating status by
# hand) — the pipeline never re-processes or silently overwrites these.
_TERMINAL_STATUSES = {
    JobStatus.APPLIED,
    JobStatus.SKIPPED,
    JobStatus.REJECTED,
    JobStatus.INTERVIEW,
    JobStatus.OFFER,
}

APPLICATIONS_DIR = PROJECT_ROOT / "applications"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "job"


@dataclass
class ProcessResult:
    job_id: int
    final_status: str
    qualified: bool
    application_id: int | None = None
    reason: str | None = None


def process_job(
    job_id: int,
    *,
    master_resume: MasterResume | None = None,
    db_path: str | None = None,
    analysis_provider=None,
    tailoring_provider=None,
    cover_letter_provider=None,
    qc_provider=None,
    applications_dir: Path | None = None,
) -> ProcessResult:
    """
    Run one job through cheap filter -> AI analysis -> scoring -> (if
    qualified) tailoring -> cover letter -> QC -> PDF -> DB writes.

    Never raises on an AI failure or a rejection — those are normal,
    expected outcomes reflected in the returned ProcessResult and the job's
    status, not exceptions. Only truly unexpected errors (e.g. the job_id
    not existing) raise.

    The four *_provider params exist for testing (inject a fake AIProvider
    instead of hitting real Ollama) — leave them None in normal use to fall
    back to the configured default provider for each stage.
    """
    conn = get_connection(db_path)
    try:
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(f"No job with id={job_id}")

        if job["status"] in _TERMINAL_STATUSES:
            return ProcessResult(job_id, job["status"], qualified=False, reason="Job is in a terminal, user-controlled status — not re-processed")

        resume = master_resume or load_master_resume()

        cheap_result = run_cheap_filter(job["title"], job["description"], job["location"] or "")
        if not cheap_result.passed:
            reason = "; ".join(cheap_result.reasons) or f"Rejected by cheap filter ({cheap_result.rejection_category})"
            update_job_status(conn, job_id, JobStatus.REJECTED, rejection_reason=reason)
            conn.commit()
            logger.info("[FILTER] job=%d rejected: %s", job_id, reason)
            return ProcessResult(job_id, JobStatus.REJECTED, qualified=False, reason=reason)

        update_job_status(conn, job_id, JobStatus.ANALYZING)
        conn.commit()

        try:
            analysis = analyze_job(
                title=job["title"], description=job["description"], candidate_skills=resume.skills, provider=analysis_provider
            )
        except AIResponseError as exc:
            logger.warning("[AI] job=%d analysis failed, leaving for retry: %s", job_id, exc)
            return ProcessResult(job_id, JobStatus.ANALYZING, qualified=False, reason=f"AI analysis failed: {exc}")

        score = score_job(cheap_result, analysis)
        update_job_analysis(
            conn,
            job_id,
            category=analysis.category,
            fit_score=score.total_score,
            experience_required=analysis.experience_required,
            ai_analysis_json=analysis.model_dump_json(),
        )
        conn.commit()

        if score.vetoed or not score.qualified:
            reason = score.veto_reason or f"Fit score {score.total_score} below threshold ({settings.min_fit_score})"
            update_job_status(conn, job_id, JobStatus.REJECTED, rejection_reason=reason)
            conn.commit()
            logger.info("[AI] job=%d rejected after scoring: %s", job_id, reason)
            return ProcessResult(job_id, JobStatus.REJECTED, qualified=False, reason=reason)

        update_job_status(conn, job_id, JobStatus.QUALIFIED)
        conn.commit()
        logger.info("[AI] job=%d qualified: fit_score=%d", job_id, score.total_score)

        try:
            qc_result = run_quality_control_with_correction(
                resume,
                title=job["title"],
                company=job["company"] or "",
                description=job["description"],
                tailoring_provider=tailoring_provider,
                cover_letter_provider=cover_letter_provider,
                qc_provider=qc_provider,
            )
        except AIResponseError as exc:
            logger.warning("[APPLICATION] job=%d generation failed, leaving QUALIFIED for retry: %s", job_id, exc)
            return ProcessResult(job_id, JobStatus.QUALIFIED, qualified=True, reason=f"Application generation failed: {exc}")

        application_id = _persist_application(conn, job, qc_result, applications_dir)
        conn.commit()

        # TYPE_B (manual) always, until Phase 16 adds a legitimate, ToS-
        # compliant TYPE_A (assisted ATS) automation path.
        final_status = JobStatus.READY_TO_APPLY if qc_result.passed else JobStatus.QUALIFIED
        update_job_status(conn, job_id, final_status)
        conn.commit()
        logger.info(
            "[APPLICATION] job=%d %s (qc_passed=%s)",
            job_id,
            "generated, ready to apply" if qc_result.passed else "generated but flagged for manual review",
            qc_result.passed,
        )

        return ProcessResult(job_id, final_status, qualified=True, application_id=application_id)
    finally:
        conn.close()


def _persist_application(conn, job, qc_result, applications_dir: Path | None = None) -> int:
    slug = _slugify(f"{job['company'] or 'unknown'}-{job['title']}")
    out_dir = (applications_dir or APPLICATIONS_DIR) / slug

    resume_pdf_path = render_resume_pdf(qc_result.tailored_resume, out_dir / "resume.pdf")
    cover_letter_pdf_path = render_cover_letter_pdf(
        qc_result.cover_letter, qc_result.tailored_resume, out_dir / "cover-letter.pdf", job_title=job["title"], company=job["company"] or ""
    )
    render_cover_letter_markdown(
        qc_result.cover_letter, qc_result.tailored_resume, out_dir / "cover-letter.md", job_title=job["title"], company=job["company"] or ""
    )
    (out_dir / "job.json").write_text(json.dumps(dict(job), indent=2, default=str))

    resume_version_id = insert_resume_version(
        conn,
        job["id"],
        modifications_json=qc_result.tailored_resume.model_dump_json(),
        rendered_html=None,
        pdf_path=str(resume_pdf_path),
    )
    cover_letter_id = insert_cover_letter(
        conn,
        job["id"],
        markdown_path=str(out_dir / "cover-letter.md"),
        pdf_path=str(cover_letter_pdf_path),
        word_count=len(qc_result.cover_letter.split()),
    )

    application_status = JobStatus.READY_TO_APPLY if qc_result.passed else JobStatus.QUALIFIED
    notes = None if qc_result.passed else "Flagged for manual review: " + "; ".join(qc_result.qc.issues)

    return insert_application(
        conn,
        {
            "job_id": job["id"],
            "resume_version_id": resume_version_id,
            "cover_letter_id": cover_letter_id,
            "resume_path": str(resume_pdf_path),
            "cover_letter_path": str(cover_letter_pdf_path),
            "status": application_status,
            "qc_passed": qc_result.passed,
            "qc_issues_json": json.dumps(qc_result.qc.issues),
            "notes": notes,
        },
    )


def process_new_jobs(*, db_path: str | None = None) -> dict[str, int]:
    """
    Process every job currently in NEW status. Returns aggregate counts and
    logs them in the format spec section 30 asks for.
    """
    from src.database.jobs_repo import list_jobs_by_status

    conn = get_connection(db_path)
    try:
        new_jobs = list_jobs_by_status(conn, JobStatus.NEW)
    finally:
        conn.close()

    counts = {"processed": 0, "rejected": 0, "qualified": 0, "ready_to_apply": 0, "flagged_for_review": 0, "left_for_retry": 0}
    resume = load_master_resume()

    for job in new_jobs:
        result = process_job(job["id"], master_resume=resume, db_path=db_path)
        counts["processed"] += 1
        if result.final_status == JobStatus.REJECTED:
            counts["rejected"] += 1
        elif result.final_status == JobStatus.READY_TO_APPLY:
            counts["qualified"] += 1
            counts["ready_to_apply"] += 1
        elif result.final_status == JobStatus.QUALIFIED and result.application_id is not None:
            counts["qualified"] += 1
            counts["flagged_for_review"] += 1
        else:
            counts["left_for_retry"] += 1

    logger.info(
        "[AI] Analyzed: %d | Qualified: %d\n[APPLICATION] Generated: %d | Flagged for review: %d",
        counts["processed"],
        counts["qualified"],
        counts["ready_to_apply"],
        counts["flagged_for_review"],
    )
    return counts
