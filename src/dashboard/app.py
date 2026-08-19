"""
Phase 14 — local application dashboard (spec section 21).

A small, server-rendered Flask app — no JS framework, no CDN assets, no
external calls of any kind. Bound to 127.0.0.1 only (see run_dashboard()),
same "never expose locally-run tools publicly" rule already applied to
n8n. Reads/writes the same SQLite database every other phase uses.

This is deliberately a thin presentation + action layer: it never talks to
Ollama, tailors a resume, or writes a cover letter itself — all of that
already happened in src/pipeline/process_job.py. The one exception is the
"Process" buttons, which just call that existing pipeline synchronously;
they don't duplicate any of its logic.
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for

from src.config import PROJECT_ROOT, settings
from src.database.applications_repo import get_application_by_job_id, update_application_status
from src.database.db import get_connection, init_db
from src.database.jobs_repo import get_job, list_all_jobs, update_job_status
from src.database.models import JobStatus
from src.sources.manual_import import normalize_manual_job

# Display order for the dashboard's status columns (spec section 21 lists
# NEW/QUALIFIED/READY_TO_APPLY/APPLIED/INTERVIEW/REJECTED/SKIPPED; ANALYZING
# and OFFER are included too since they're real states a job can be in).
STATUS_COLUMNS = [
    JobStatus.NEW,
    JobStatus.ANALYZING,
    JobStatus.QUALIFIED,
    JobStatus.READY_TO_APPLY,
    JobStatus.APPLIED,
    JobStatus.INTERVIEW,
    JobStatus.OFFER,
    JobStatus.REJECTED,
    JobStatus.SKIPPED,
]


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "local-only-dashboard-no-real-sessions"  # no auth/session data of consequence
    app.config["DB_PATH"] = db_path

    @app.route("/")
    def index():
        conn = get_connection(app.config["DB_PATH"])
        try:
            all_jobs = list_all_jobs(conn)
        finally:
            conn.close()

        columns = {status: [] for status in STATUS_COLUMNS}
        for job in all_jobs:
            columns.setdefault(job["status"], []).append(job)

        return render_template("dashboard.html", columns=columns, status_order=STATUS_COLUMNS)

    @app.route("/job/<int:job_id>")
    def job_detail(job_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            job = get_job(conn, job_id)
            if job is None:
                abort(404)
            application = get_application_by_job_id(conn, job_id)
        finally:
            conn.close()

        analysis = {}
        if job["ai_analysis_json"]:
            try:
                analysis = json.loads(job["ai_analysis_json"])
            except (TypeError, ValueError):
                analysis = {}

        qc_issues = []
        if application and application["qc_issues_json"]:
            try:
                qc_issues = json.loads(application["qc_issues_json"])
            except (TypeError, ValueError):
                qc_issues = []

        return render_template(
            "job_detail.html", job=job, application=application, analysis=analysis, qc_issues=qc_issues
        )

    @app.route("/job/<int:job_id>/mark-applied", methods=["POST"])
    def mark_applied(job_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            job = get_job(conn, job_id)
            if job is None:
                abort(404)
            update_job_status(conn, job_id, JobStatus.APPLIED)
            application = get_application_by_job_id(conn, job_id)
            if application is not None:
                from datetime import date

                update_application_status(conn, application["id"], JobStatus.APPLIED, date_applied=date.today().isoformat())
            conn.commit()
        finally:
            conn.close()
        flash(f"Marked {job['title']} at {job['company']} as APPLIED.")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/job/<int:job_id>/skip", methods=["POST"])
    def skip(job_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            job = get_job(conn, job_id)
            if job is None:
                abort(404)
            update_job_status(conn, job_id, JobStatus.SKIPPED)
            conn.commit()
        finally:
            conn.close()
        flash(f"Skipped {job['title']} at {job['company']}.")
        return redirect(url_for("index"))

    @app.route("/job/<int:job_id>/process", methods=["POST"])
    def process(job_id: int):
        # Synchronous — this blocks on real AI calls (can take up to ~1-2
        # minutes). Acceptable for a local, single-user tool processing a
        # handful of jobs at a time; a background queue is future work if
        # this ever becomes a bottleneck.
        from src.pipeline.process_job import process_job

        result = process_job(job_id, db_path=app.config["DB_PATH"])
        flash(f"Processed: {result.final_status}" + (f" — {result.reason}" if result.reason else ""))
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/job/<int:job_id>/resume.pdf")
    def resume_pdf(job_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            application = get_application_by_job_id(conn, job_id)
        finally:
            conn.close()
        if application is None or not application["resume_path"] or not Path(application["resume_path"]).exists():
            abort(404)
        return send_file(application["resume_path"], mimetype="application/pdf")

    @app.route("/job/<int:job_id>/cover-letter.pdf")
    def cover_letter_pdf(job_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            application = get_application_by_job_id(conn, job_id)
        finally:
            conn.close()
        if application is None or not application["cover_letter_path"] or not Path(application["cover_letter_path"]).exists():
            abort(404)
        return send_file(application["cover_letter_path"], mimetype="application/pdf")

    @app.route("/import", methods=["GET", "POST"])
    def import_job():
        if request.method == "GET":
            return render_template("import.html")

        from src.database.jobs_repo import DuplicateJobError, insert_job

        try:
            normalized = normalize_manual_job(
                title=request.form.get("title", ""),
                description=request.form.get("description", ""),
                url=request.form.get("url", ""),
                company=request.form.get("company", ""),
                location=request.form.get("location", ""),
                salary=request.form.get("salary", ""),
            )
        except ValueError as exc:
            flash(f"Could not import job: {exc}")
            return render_template("import.html"), 400

        conn = get_connection(app.config["DB_PATH"])
        try:
            try:
                job_id = insert_job(conn, normalized.to_dict())
                conn.commit()
            except DuplicateJobError as exc:
                flash("This job looks like a duplicate of an existing one.")
                return redirect(url_for("job_detail", job_id=exc.existing_job_id))
        finally:
            conn.close()

        flash(f"Imported: {normalized.title}")
        return redirect(url_for("job_detail", job_id=job_id))

    return app


def run_dashboard() -> None:
    """Entry point: `python -m src.dashboard.app`."""
    init_db()
    app = create_app()
    # 127.0.0.1 only — never 0.0.0.0. Matches the same local-only rule
    # already applied to n8n (see docker-compose.yml).
    app.run(host=settings.dashboard_host, port=settings.dashboard_port, debug=False)


if __name__ == "__main__":
    run_dashboard()
