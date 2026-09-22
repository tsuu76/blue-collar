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
import logging
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from src.browser_assist.classify import detect_platform
from src.browser_assist.reachability import check_url_reachable
from src.config import PROJECT_ROOT, settings
from src.database.applications_repo import get_application_by_job_id, update_application_status
from src.database.db import get_connection, init_db
from src.database.jobs_repo import get_job, list_all_jobs, update_job_status
from src.database.models import JobStatus, OutreachStatus
from src.database.outreach_repo import (
    approve_message,
    approve_messages,
    get_company,
    get_message,
    list_companies,
    list_messages_for_company,
    reject_message,
    set_do_not_contact,
    update_company_contact_email,
    update_message_content,
)
from src.sources.manual_import import normalize_manual_job

from .activity_view import build_activity
from .jobs_query import (
    JOBS_STATUS_FILTER,
    count_by_status,
    count_since,
    distinct_locations,
    distinct_sources,
    last_job_found_at,
    parse_jobs_query,
    recent_jobs,
    search_jobs,
    total_jobs,
)
from .sources_view import (
    build_source_rows,
    find_by_slug,
    friendly_source_label,
    jobs_for_company,
    platform_label,
    slugify_company,
)

app_logger = logging.getLogger("job_hunter.dashboard")

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

# --- Outreach UI ------------------------------------------------------------
# Outreach runs end to end on its own now, so this page reports rather than
# decides: it shows what each company's outcome was and why. It generates,
# researches and sends nothing itself — same thin-presentation rule the job
# board follows. The approve/reject routes below still exist as a manual
# fallback for anything the automatic run left behind, but nothing on this
# page asks the user to approve an email before it goes out.

# Reading order: what happened, then what didn't.
OUTREACH_MESSAGE_COLUMNS = [
    OutreachStatus.SENT,
    OutreachStatus.FAILED,
    OutreachStatus.DRAFT,
    OutreachStatus.APPROVED,
    OutreachStatus.REJECTED,
]

# Reasons a company cannot produce an outreach email, in the order they're
# worth telling the user about. Each is derived from stored data at render
# time rather than cached, so the answer is never stale.
_STATE_DO_NOT_CONTACT = "DO_NOT_CONTACT"
_STATE_NO_CONTACT_EMAIL = "NEEDS_CONTACT"
_STATE_NOT_RESEARCHED = "NOT_RESEARCHED"
_STATE_NO_POSTINGS = "NO_POSTINGS"
_STATE_NO_OVERLAP = "NO_OVERLAP"
_STATE_READY_TO_DRAFT = "READY_TO_DRAFT"
_STATE_HAS_DRAFT = "DRAFT_READY"
_STATE_GATE_FAILED = "GATE_FAILED"
_STATE_APPROVED = "APPROVED"
_STATE_SENT = "SENT"
_STATE_FAILED = "FAILED"

_STATE_LABELS = {
    _STATE_DO_NOT_CONTACT: "Do not contact",
    _STATE_NO_CONTACT_EMAIL: "Needs contact",
    _STATE_NOT_RESEARCHED: "Not researched yet",
    _STATE_NO_POSTINGS: "No postings found",
    _STATE_NO_OVERLAP: "No relevant opening",
    _STATE_READY_TO_DRAFT: "Ready to contact",
    _STATE_HAS_DRAFT: "Written, not sent",
    _STATE_GATE_FAILED: "Gate failed",
    _STATE_APPROVED: "Cleared, not yet sent",
    _STATE_SENT: "Sent",
    _STATE_FAILED: "Failed to send",
}

# Design-system pill classes for each state (see .pill--* in style.css).
_STATE_PILLS = {
    _STATE_DO_NOT_CONTACT: "pill--crimson",
    _STATE_NO_CONTACT_EMAIL: "pill--ember",
    _STATE_NOT_RESEARCHED: "pill--steel",
    _STATE_NO_POSTINGS: "",
    _STATE_NO_OVERLAP: "",
    _STATE_READY_TO_DRAFT: "pill--steel",
    _STATE_HAS_DRAFT: "pill--ember",
    _STATE_GATE_FAILED: "pill--crimson",
    _STATE_APPROVED: "pill--steel",
    _STATE_SENT: "pill--sage",
    _STATE_FAILED: "pill--crimson",
}


def _load_json(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _regate(conn, message_id: int):
    """
    Re-run the deterministic quality gate against a message's current text
    and persist the verdict, immediately before approval is attempted.

    Returns the GateResult, or None if the gate could not run at all (no
    master resume on disk, say). None means "unknown", and the caller falls
    back to approve_message's own gate_passed check — which refuses anything
    not already carrying a recorded PASS, so an inability to re-gate can
    never widen what gets approved.
    """
    from src.outreach.quality_gate import gate_and_record
    from src.resume.store import load_master_resume

    try:
        return gate_and_record(conn, message_id, load_master_resume())
    except (FileNotFoundError, ValueError) as exc:
        app_logger.warning("Could not re-run the quality gate for message %d: %s", message_id, exc)
        return None


def company_state(company, research: dict, messages: list) -> dict:
    """
    Work out where a company stands and, when nothing was sent, why.

    Everything here is derived from what is actually stored — a reason is
    never shown unless the data supports it, and a company is never presented
    as contactable when it isn't. Returns {state, label, pill, reason}.

    Ordered most-settled-first, so a company that has been emailed reads as
    "sent" no matter what else is in its history.
    """
    statuses = {message["status"] for message in messages}
    drafts = [m for m in messages if m["status"] == OutreachStatus.DRAFT]
    reason = ""

    if OutreachStatus.SENT in statuses:
        state = _STATE_SENT
        reason = "already contacted — this company will not be emailed again"
    elif OutreachStatus.FAILED in statuses:
        state = _STATE_FAILED
        failed = [m for m in messages if m["status"] == OutreachStatus.FAILED]
        reason = failed[-1]["last_error"] or "the mail server did not accept this message"
    elif OutreachStatus.APPROVED in statuses:
        state = _STATE_APPROVED
    elif drafts:
        # A draft that exists but never passed the gate is a different
        # situation from one waiting on the next run, and says so.
        blocked = [m for m in drafts if m["gate_passed"] != 1]
        if blocked:
            state = _STATE_GATE_FAILED
            reason = "; ".join(_load_json(blocked[-1]["gate_reasons_json"], [])) or (
                "this email did not pass the quality gate, so it cannot be sent"
            )
        else:
            state = _STATE_HAS_DRAFT
            reason = "written and cleared — it will go out on the next run"
    else:
        state = None
    if company["do_not_contact"]:
        state = _STATE_DO_NOT_CONTACT
        reason = company["do_not_contact_reason"] or "marked do-not-contact"
    elif state is None:
        # No message yet — say what's standing in the way of drafting one.
        if not research:
            state = _STATE_NOT_RESEARCHED
            reason = "this company hasn't been researched yet"
        elif not research.get("posting_count"):
            state = _STATE_NO_POSTINGS
            reason = research.get("error") or (
                "no current public postings were found on this company's job board"
            )
        elif not (company["contact_email"] or "").strip():
            state = _STATE_NO_CONTACT_EMAIL
            reason = (
                "no verified contact email was found on this company's own pages or in "
                "their postings — nothing is ever guessed, so add one below to reach them"
            )
        else:
            state = _STATE_READY_TO_DRAFT

    # A researched, draftable-looking company still needs a contact address.
    if state == _STATE_READY_TO_DRAFT and not (company["contact_email"] or "").strip():
        state = _STATE_NO_CONTACT_EMAIL
        reason = (
            "no verified contact email was found on this company's own pages or in "
            "their postings — nothing is ever guessed, so add one below to reach them"
        )

    return {
        "state": state,
        "label": _STATE_LABELS.get(state, state),
        "pill": _STATE_PILLS.get(state, ""),
        "reason": reason,
    }


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


def _humanize_iso(iso: str | None) -> str:
    """
    Turn an ISO datetime like "2026-09-14 03:12:44" into a compact,
    human-friendly form ("14 Sep 2026, 03:12"). Never raises: the
    landing page must render even when the DB is empty or the value
    is malformed, so any unparseable input yields an empty string.
    """
    from datetime import datetime

    if not iso:
        return ""
    text = iso.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%-d %b %Y, %H:%M") if "%H" in fmt else dt.strftime("%-d %b %Y")
        except ValueError:
            continue
    return text


def _today_start_iso() -> str:
    """
    Start-of-day timestamp for "new today" comparisons. Uses local
    time — the same clock every date_found value was recorded with.
    """
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d 00:00:00")


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "local-only-dashboard-no-real-sessions"  # no auth/session data of consequence
    app.config["DB_PATH"] = db_path

    # Jinja globals — make the friendly-source label and the slug helper
    # available inside every template without needing to pass them from
    # each view. Templates read them; they never mutate anything.
    app.jinja_env.globals["friendly_source"] = friendly_source_label
    app.jinja_env.globals["platform_label"] = platform_label
    app.jinja_env.globals["slugify"] = slugify_company

    @app.route("/")
    def index():
        """Landing dashboard — overview + recent jobs, primary action to find more."""
        from src.config import settings

        conn = get_connection(app.config["DB_PATH"])
        try:
            # Client-facing counts and lists default to the app's target
            # locations. See jobs_query for the SQL side and the pinned
            # target-locations-are-a-hard-ui-default memory for why.
            total = total_jobs(conn)
            last_iso = last_job_found_at(conn)
            new_today = count_since(conn, _today_start_iso())
            recent = recent_jobs(conn, limit=8)
            source_rows = build_source_rows(conn)
        finally:
            conn.close()

        return render_template(
            "home.html",
            active_nav="dashboard",
            total=total,
            new_today=new_today,
            last_updated_human=_humanize_iso(last_iso),
            recent=recent,
            source_count=len(source_rows),
            active_source_count=sum(1 for r in source_rows if r.enabled),
            target_locations=list(settings.target_locations),
        )

    @app.route("/board")
    def board_page():
        """
        The pipeline board (status columns) — the pre-existing dashboard
        view, preserved verbatim. Kept reachable so bookmarks, tests, and
        anyone used to the pipeline flow don't lose it. `/` is now the
        client-facing landing page.
        """
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
            active_nav="board",
            columns=columns,
            status_order=STATUS_COLUMNS,
            manual_cards=manual_cards,
        )

    @app.route("/jobs")
    def jobs_page():
        """
        Job search + filter list. The Jobs page is the core of the
        client-facing UI — every filter here is backed by real database
        values (see jobs_query.py), never fabricated.

        Scope defaults to `target` (Sydney/NSW/Remote AU) — the target-
        region rule the whole client UI honours. `?scope=anywhere`
        widens the view for the one-off case where the user wants it.
        """
        from src.config import settings

        query = parse_jobs_query(request.args)
        target_only = query.scope != "anywhere"

        conn = get_connection(app.config["DB_PATH"])
        try:
            jobs = search_jobs(conn, query)
            # The Location dropdown only offers values the current
            # scope actually contains — under the default target scope
            # it lists Sydney/NSW/Remote AU locations, so the dropdown
            # can never present a "Toronto" option that the results
            # would then have to hide.
            locations = distinct_locations(conn, target_only=target_only)
            source_tokens = distinct_sources(conn)
        finally:
            conn.close()

        # Friendly labels for the Source filter dropdown. `distinct_sources`
        # gives raw tokens ordered by frequency; we render each with the
        # same friendly-label helper the rest of the UI uses.
        sources = [(token, friendly_source_label(token)) for token in source_tokens]

        return render_template(
            "jobs.html",
            active_nav="jobs",
            jobs=jobs,
            query=query,
            locations=locations,
            sources=sources,
            statuses=JOBS_STATUS_FILTER,
            result_count=len(jobs),
            target_locations=list(settings.target_locations),
            target_only=target_only,
        )

    @app.route("/sources")
    def sources_page():
        conn = get_connection(app.config["DB_PATH"])
        try:
            rows = build_source_rows(conn)
        finally:
            conn.close()
        return render_template(
            "sources.html",
            active_nav="sources",
            rows=rows,
            active_count=sum(1 for r in rows if r.enabled),
        )

    @app.route("/sources/<slug>/add-to-outreach", methods=["POST"])
    def source_add_to_outreach(slug: str):
        """
        Copy a configured employer from config/employers.json into
        config/outreach_companies.json, so the outreach pipeline will
        consider them on its next run. Nothing is fabricated — the
        entry already exists in the user's own employers config; this
        route is a one-click alternative to editing the file by hand.

        Idempotent on (platform, identifier), which is what Ishmam
        asked for: a double-click can't create a duplicate, and a
        display-name variation ("SafetyCulture" vs "Safety Culture")
        can't slip past by looking like a different row.
        """
        from src.job_discovery.registry import load_employers
        from src.outreach.targets import add_target

        # Rebuild the SourceRow list only to resolve the slug — the
        # slug never appears in the underlying data.
        conn = get_connection(app.config["DB_PATH"])
        try:
            rows = build_source_rows(conn)
        finally:
            conn.close()
        row = find_by_slug(rows, slug)
        if row is None:
            abort(404)

        employers = load_employers()
        match = next(
            (
                e
                for e in employers
                if e.company == row.company
                and e.platform == row.platform
                and e.identifier == row.identifier
            ),
            None,
        )
        if match is None:
            flash(f"Could not find {row.company} in employers.json to copy over.")
            return redirect(url_for("source_detail_page", slug=slug))

        added, reason = add_target({
            "company": match.company,
            "platform": match.platform,
            "identifier": match.identifier,
            "website": "",
            "contact_email": "",
            "notes": f"Copied from employers.json via the Sources page.",
            "enabled": True,
        })
        if added:
            flash(f"{match.company} added to outreach.")
        elif reason == "already_present":
            flash(f"{match.company} is already on the outreach list.")
        else:
            flash(f"Could not add {match.company} to outreach: {reason}.")
        return redirect(url_for("source_detail_page", slug=slug))

    @app.route("/sources/<slug>")
    def source_detail_page(slug: str):
        conn = get_connection(app.config["DB_PATH"])
        try:
            # Default to the target-region view for consistency with
            # /sources; the count on the row and the jobs listed below
            # then agree with each other. The pipeline board is where
            # the reviewer sees rejected/overseas jobs.
            rows = build_source_rows(conn)
            row = find_by_slug(rows, slug)
            if row is None:
                abort(404)
            jobs = jobs_for_company(conn, row.company, limit=50)
        finally:
            conn.close()

        # Pass the raw source token if every job for this company shares
        # one — that lets the "See all" link deep-link to /jobs?source=…
        # without inventing a filter that doesn't fit the data.
        source_tokens = {j["source"] for j in jobs if j["source"]}
        source_token = next(iter(source_tokens)) if len(source_tokens) == 1 else ""

        return render_template(
            "source_detail.html",
            active_nav="sources",
            row=row,
            jobs=jobs,
            source_token=source_token,
        )

    @app.route("/activity")
    def activity_page():
        conn = get_connection(app.config["DB_PATH"])
        try:
            items = build_activity(conn, limit=40)
        finally:
            conn.close()
        return render_template(
            "activity.html",
            active_nav="activity",
            items=items,
        )

    @app.route("/jobs/<int:job_id>")
    def jobs_detail_alias(job_id: int):
        """
        `/jobs/<id>` is the client-facing path; the pre-existing
        `/job/<id>` route stays too, so links from the pipeline board
        and from any earlier bookmark keep working. Both render the
        same detail page.
        """
        return redirect(url_for("job_detail", job_id=job_id))

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
        """
        The "Find new jobs" button's non-JS fallback. Runs the same
        discovery pipeline; the flash message is written in the
        user-facing tone the landing page uses (no crawler jargon, no
        raw exception messages), while the technical detail stays in
        the application log for debugging.
        """
        from src.job_discovery.run_discovery import run_discovery

        try:
            result = run_discovery(db_path=app.config["DB_PATH"], process=True)
        except Exception:  # noqa: BLE001
            app.logger.exception("Discovery run failed")
            flash("Some sources need attention. Try again shortly.")
            return redirect(url_for("index"))

        inserted = int(result.get("inserted", 0))
        if inserted == 0:
            flash("You're up to date. Nothing new since the last check.")
        else:
            noun = "job" if inserted == 1 else "jobs"
            flash(f"{inserted} new {noun} found.")
        return redirect(url_for("index"))

    # --- Outreach review ----------------------------------------------------
    # Read + approve only. Nothing here generates, researches, or sends —
    # approving a draft changes DRAFT to APPROVED and does nothing else.

    @app.route("/outreach")
    def outreach():
        conn = get_connection(app.config["DB_PATH"])
        try:
            companies = list_companies(conn)
            entries = []
            message_columns = {status: [] for status in OUTREACH_MESSAGE_COLUMNS}
            for company in companies:
                messages = list_messages_for_company(conn, company["id"])
                research = _load_json(company["research_json"], {})
                # Picker inventory + X handle come from the two new
                # columns added in this increment. Passed to Jinja as
                # plain values so the template doesn't do JSON parsing.
                discovered_contacts = _load_json(
                    company["discovered_contacts_json"], []
                )
                entry = {
                    "company": company,
                    "research": research,
                    "messages": messages,
                    "state": company_state(company, research, messages),
                    "discovered_contacts": discovered_contacts,
                }
                entries.append(entry)
                for message in messages:
                    if message["status"] in message_columns:
                        message_columns[message["status"]].append(
                            {"message": message, "company": company}
                        )
        finally:
            conn.close()

        return render_template(
            "outreach.html",
            entries=entries,
            message_columns=message_columns,
            message_status_order=OUTREACH_MESSAGE_COLUMNS,
            draft_count=len(message_columns[OutreachStatus.DRAFT]),
        )

    @app.route("/outreach/company/<int:company_id>/contact", methods=["POST"])
    def outreach_set_contact(company_id: int):
        """
        Set or correct a company's verified contact address.

        Typed by the user, never discovered: the system does not guess
        addresses, so this form is the only way one is ever recorded.
        """
        conn = get_connection(app.config["DB_PATH"])
        try:
            company = get_company(conn, company_id)
            if company is None:
                abort(404)
            update_company_contact_email(conn, company_id, request.form.get("contact_email", ""))
            conn.commit()
        finally:
            conn.close()
        flash(f"Contact address updated for {company['name']}.")
        return redirect(url_for("outreach"))

    @app.route("/outreach/company/<int:company_id>/do-not-contact", methods=["POST"])
    def outreach_do_not_contact(company_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            company = get_company(conn, company_id)
            if company is None:
                abort(404)
            blocked = set_do_not_contact(conn, company_id, "marked do-not-contact from the dashboard")
            conn.commit()
        finally:
            conn.close()
        flash(f"{company['name']} marked do-not-contact." + (f" {blocked} queued message(s) blocked." if blocked else ""))
        return redirect(url_for("outreach"))

    @app.route("/outreach/run", methods=["POST"])
    def outreach_run():
        """
        Run the whole outreach pathway now: research, contact discovery,
        drafting, the quality gate, and — unless OUTREACH_DRY_RUN is on —
        sending. Synchronous and slow (it runs real AI and real SMTP), same
        tradeoff as the job board's "Process this job" button.

        This route decides nothing. Every safety check lives in the pipeline
        and the sender, so what happens here is exactly what happens on the
        command line.
        """
        from src.outreach.pipeline import run_outreach

        try:
            result = run_outreach(db_path=app.config["DB_PATH"])
        except Exception as exc:  # noqa: BLE001 — surface it, don't 500 the page
            app_logger.exception("Outreach run failed")
            flash(f"Outreach run failed: {exc}")
            return redirect(url_for("outreach"))

        summary = (
            f"Outreach: {result.sent} sent, {result.drafted} written but not sent, "
            f"{result.skipped} skipped, {result.failed} failed."
        )
        if not result.sending_enabled:
            summary += f" Nothing was sent — {result.sending_blocked_reason}"
        flash(summary)
        return redirect(url_for("outreach"))

    @app.route("/outreach/draft/<int:message_id>")
    def outreach_draft(message_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            message = get_message(conn, message_id)
            if message is None:
                abort(404)
            company = get_company(conn, message["company_id"])
        finally:
            conn.close()

        analysis_blob = _load_json(message["analysis_json"], {})
        research = _load_json(company["research_json"], {}) if company else {}
        return render_template(
            "outreach_draft.html",
            message=message,
            company=company,
            research=research,
            # The verified analysis this email was written from, plus what
            # verification discarded — both worth seeing while reviewing.
            analysis=analysis_blob.get("analysis", {}),
            dropped_company_terms=analysis_blob.get("dropped_company_terms", []),
            dropped_candidate_claims=analysis_blob.get("dropped_candidate_claims", []),
        )

    @app.route("/outreach/draft/<int:message_id>/edit", methods=["POST"])
    def outreach_edit_draft(message_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            if get_message(conn, message_id) is None:
                abort(404)
            updated = update_message_content(
                conn,
                message_id,
                subject=request.form.get("subject", ""),
                body=request.form.get("body", ""),
            )
            conn.commit()
        finally:
            conn.close()

        if updated:
            flash("Draft updated.")
        else:
            # update_message_content only touches DRAFT rows — editing after
            # approval would make the approval meaningless.
            flash("This draft can no longer be edited — only drafts awaiting review can be changed.")
        return redirect(url_for("outreach_draft", message_id=message_id))

    @app.route("/outreach/draft/<int:message_id>/approve", methods=["POST"])
    def outreach_approve_draft(message_id: int):
        """
        Approve one draft. This does NOT send anything — it only moves the
        message from DRAFT to APPROVED.

        The deterministic quality gate is re-run first, against the draft's
        CURRENT text. That matters because editing clears the previous
        verdict: an edited draft is re-checked here rather than inheriting
        approval its earlier wording earned.
        """
        conn = get_connection(app.config["DB_PATH"])
        try:
            if get_message(conn, message_id) is None:
                abort(404)
            gate = _regate(conn, message_id)
            approved = approve_message(conn, message_id) if (gate is None or gate.passed) else False
            conn.commit()
        finally:
            conn.close()

        if approved:
            flash("Draft approved. Nothing has been sent — approval only marks it ready.")
        elif gate is not None and not gate.passed:
            flash("Quality gate FAILED — not approved. " + "; ".join(gate.reasons))
        else:
            flash("That draft could not be approved (it may already be approved, sent, or rejected).")
        return redirect(url_for("outreach_draft", message_id=message_id))

    @app.route("/outreach/draft/<int:message_id>/reject", methods=["POST"])
    def outreach_reject_draft(message_id: int):
        conn = get_connection(app.config["DB_PATH"])
        try:
            if get_message(conn, message_id) is None:
                abort(404)
            rejected = reject_message(conn, message_id)
            conn.commit()
        finally:
            conn.close()

        flash("Draft rejected." if rejected else "That draft could not be rejected.")
        return redirect(url_for("outreach"))

    @app.route("/outreach/approve-selected", methods=["POST"])
    def outreach_approve_selected():
        """
        Bulk approve. Same per-message guard as the single action — an id
        that isn't an eligible draft is skipped rather than failing the
        batch. Still sends nothing.
        """
        raw_ids = request.form.getlist("message_ids")
        message_ids = []
        for raw in raw_ids:
            try:
                message_ids.append(int(raw))
            except (TypeError, ValueError):
                continue

        if not message_ids:
            flash("No drafts selected.")
            return redirect(url_for("outreach"))

        conn = get_connection(app.config["DB_PATH"])
        try:
            # Re-gate each one against its current text before approving, so
            # a bulk action can't wave through a draft that was edited after
            # it last passed.
            for candidate_id in message_ids:
                _regate(conn, candidate_id)
            approved = approve_messages(conn, message_ids)
            conn.commit()
        finally:
            conn.close()

        skipped = len(message_ids) - len(approved)
        message = f"Approved {len(approved)} draft{'s' if len(approved) != 1 else ''}. Nothing has been sent."
        if skipped:
            message += f" {skipped} skipped (failed the quality gate, or not an eligible draft)."
        flash(message)
        return redirect(url_for("outreach"))

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
