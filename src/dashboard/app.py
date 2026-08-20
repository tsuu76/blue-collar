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

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from src.browser_assist.classify import detect_platform
from src.browser_assist.reachability import check_url_reachable
from src.config import PROJECT_ROOT, settings
from src.database.applications_repo import get_application_by_job_id, update_application_status
from src.database.db import get_connection, init_db
from src.database.jobs_repo import get_job, list_all_jobs, update_job_status
from src.database.models import JobStatus
from src.sources.manual_import import normalize_manual_job

# Display order for the dashboard's status columns — ordered by how much
# action the user can take, not by pipeline sequence. READY_TO_APPLY leads
# because those jobs have a tailored resume/cover letter waiting and only
# need the human to go submit the form; everything the user can act on
# immediately is therefore visible first, above the fold.
#
# REJECTED is deliberately absent. Rejected jobs are still discovered,
# still filtered, still written to the database and still readable at
# /job/<id> — nothing about the rejection logic or storage changes. They
# are simply not shown on the board, because a column of jobs the user
# can't act on (946 of them at the time of writing) buried the ones they
# can. index() builds its `columns` dict from this list and the template
# iterates it, so omitting a status here hides that column and nothing
# else. SKIPPED is kept, last: unlike a rejection, skipping is the user's
# own decision and worth being able to see.
STATUS_COLUMNS = [
    JobStatus.READY_TO_APPLY,
    JobStatus.QUALIFIED,
    JobStatus.ANALYZING,
    JobStatus.NEW,
    JobStatus.APPLIED,
    JobStatus.INTERVIEW,
    JobStatus.OFFER,
    JobStatus.SKIPPED,
]

# Every job in this tool is TYPE_B/manual — src/browser_assist/classify.py
# always returns TYPE_B, deliberately, since automated form-filling is never
# attempted (spec section 28). READY_TO_APPLY is the status a job reaches
# once it has a tailored resume/cover letter and is waiting on the human to
# actually go fill in and submit the form, so that's "the jobs requiring
# manual application" for the CardSwap stack on the dashboard.
_MANUAL_APPLICATION_STATUS = JobStatus.READY_TO_APPLY


def _manual_application_cards(jobs: list) -> list[dict]:
    """
    Build the small list of {title, company, location, href, image,
    fit_score, reason} dicts fed to the dashboard's CardSwap widget (see
    static/cardswap-react.js, built from frontend/cardswap/) via a JSON
    data island in dashboard.html, from existing job rows only — no new
    columns, no external calls, no second job-retrieval path.

    fit_score is the existing jobs.fit_score column. reason is the first
    entry of the existing ai_analysis_json.reasons list, when present —
    both already computed by the AI analysis stage (src/ai/job_analysis.py)
    long before this ever reaches the dashboard.

    Sorted by fit_score descending (highest-match first) — the React glue
    code only swaps through the top few of these, so this ordering decides
    which jobs get the eye-catching treatment, not which jobs exist.
    """
    cards = []
    for job in jobs:
        href = job["url"]
        if job["discovery_metadata_json"]:
            try:
                metadata = json.loads(job["discovery_metadata_json"])
                href = metadata.get("application_url") or href
            except (TypeError, ValueError):
                pass

        # No image column exists on jobs (see schema.sql) — every card uses
        # the same small local SVG mark rather than reaching out to any
        # external image service.
        image = url_for("static", filename="company-fallback.svg")

        reason = None
        if job["ai_analysis_json"]:
            try:
                analysis = json.loads(job["ai_analysis_json"])
                reasons = analysis.get("reasons") or []
                reason = reasons[0] if reasons else None
            except (TypeError, ValueError):
                pass

        cards.append(
            {
                "job_id": job["id"],
                "title": job["title"],
                "company": job["company"],
                "location": job["location"],
                "href": href,
                "image": image,
                "fit_score": job["fit_score"],
                "reason": reason,
            }
        )
    cards.sort(key=lambda c: c["fit_score"] if c["fit_score"] is not None else -1, reverse=True)
    return cards


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

        manual_cards = _manual_application_cards(columns.get(_MANUAL_APPLICATION_STATUS, []))

        return render_template(
            "dashboard.html",
            columns=columns,
            status_order=STATUS_COLUMNS,
            manual_cards=manual_cards,
        )

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
            "job_detail.html",
            job=job,
            application=application,
            analysis=analysis,
            qc_issues=qc_issues,
            platform=detect_platform(job["url"]),
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

    @app.route("/job/<int:job_id>/check-link", methods=["POST"])
    def check_link(job_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            job = get_job(conn, job_id)
            if job is None:
                abort(404)
        finally:
            conn.close()

        result = check_url_reachable(job["url"])
        if result.robots_disallowed:
            flash("Could not check this link — the site's robots.txt disallows automated access to it.")
        elif result.reachable:
            flash(f"Link looks reachable (HTTP {result.status_code}).")
        else:
            detail = f"HTTP {result.status_code}" if result.status_code else (result.error or "unreachable")
            flash(f"⚠ This link may be dead or expired ({detail}).")
        return redirect(url_for("job_detail", job_id=job_id))

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

    @app.route("/api/discover", methods=["POST"])
    def api_discover():
        """
        Automated discovery trigger — this is what the n8n schedule calls
        (see workflows/job-discovery.json). Runs every enabled employer
        adapter, inserts newly-found jobs, and hands them to the EXISTING
        pipeline. Returns JSON counts so n8n can log/branch on the result.

        Synchronous and potentially long-running (each qualified job hits
        real local AI). That's acceptable here: it's a local single-user
        tool, and n8n's HTTP node timeout is configurable in the workflow.
        Pass {"process": false} to only discover+insert and skip the AI
        pipeline (useful for a quick check of what discovery finds).
        """
        from src.job_discovery.run_discovery import run_discovery

        payload = request.get_json(silent=True) or {}
        process = bool(payload.get("process", True))
        try:
            result = run_discovery(db_path=app.config["DB_PATH"], process=process)
            return jsonify({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001 — always answer n8n with JSON, never an HTML error page
            app.logger.exception("Discovery run failed")
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/discover", methods=["POST"])
    def discover_now():
        """Same discovery run, triggered from the dashboard's own button."""
        from src.job_discovery.run_discovery import run_discovery

        try:
            result = run_discovery(db_path=app.config["DB_PATH"], process=True)
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("Discovery run failed")
            flash(f"Discovery failed: {exc}")
            return redirect(url_for("index"))

        pipeline = result.get("pipeline") or {}
        flash(
            f"Discovery: found {result['found']}, inserted {result['inserted']}, "
            f"{result['duplicates']} duplicates. "
            f"Pipeline: {pipeline.get('ready_to_apply', 0)} ready to apply, "
            f"{pipeline.get('rejected', 0)} rejected."
        )
        return redirect(url_for("index"))

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
