-- IT Job Hunter — SQLite schema
-- Local file database only. No server, no cloud, $0 cost.

PRAGMA foreign_keys = ON;

-- Status vocabulary used by both jobs and applications.status. Enforced in
-- application code (src/database/models.py) rather than a CHECK constraint,
-- so new statuses can be added without a migration.
--   NEW, ANALYZING, QUALIFIED, READY_TO_APPLY, APPLIED, REJECTED,
--   INTERVIEW, OFFER, SKIPPED

CREATE TABLE IF NOT EXISTS job_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,      -- e.g. "seek", "indeed", "manual_paste", "employer_career_page"
    kind            TEXT NOT NULL,             -- "manual" | "feed" | "api"
    base_url        TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source                 TEXT NOT NULL,             -- denormalized source name for quick reads
    source_job_id          TEXT,                      -- source's own ID/URL slug, if any
    url                    TEXT NOT NULL,
    title                  TEXT NOT NULL,
    company                TEXT,
    location               TEXT,
    salary                 TEXT,
    description            TEXT NOT NULL,
    date_found             TEXT NOT NULL DEFAULT (datetime('now')),
    category               TEXT,                      -- e.g. ENTRY_LEVEL_IT, MID_SENIOR_IT, NON_IT, UNCLEAR
    experience_required    TEXT,
    fit_score              INTEGER,
    status                 TEXT NOT NULL DEFAULT 'NEW',
    dedupe_hash            TEXT NOT NULL,              -- normalized hash of (title+company+location) or (url)
    rejection_reason       TEXT,                       -- set when cheap filter or AI rejects the job
    ai_analysis_json       TEXT,                       -- raw validated AI analysis result, for audit/debug
    application_type       TEXT,                       -- "TYPE_A" (assisted ATS) | "TYPE_B" (manual)
    canonical_url          TEXT,                       -- normalized url (see src/database/models.py canonicalize_url) for stronger dedup
    discovery_metadata_json TEXT,                      -- source-specific extras from automated discovery: posted_date, application_url, etc. (src/job_discovery/)
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (dedupe_hash)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_fit_score ON jobs (fit_score);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs (source);

CREATE TABLE IF NOT EXISTS resume_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    modifications_json TEXT NOT NULL,   -- the tailoring instructions applied to master_resume.json
    rendered_html   TEXT,
    pdf_path        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cover_letters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    markdown_path   TEXT,
    pdf_path        TEXT,
    word_count      INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS applications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    resume_version_id   INTEGER REFERENCES resume_versions (id),
    cover_letter_id     INTEGER REFERENCES cover_letters (id),
    resume_path         TEXT,
    cover_letter_path   TEXT,
    status              TEXT NOT NULL DEFAULT 'NEW',
    qc_passed           INTEGER,             -- 1/0/NULL — result of quality-control pass
    qc_issues_json       TEXT,               -- list of issues found, if any
    date_applied        TEXT,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (status);
CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications (job_id);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
