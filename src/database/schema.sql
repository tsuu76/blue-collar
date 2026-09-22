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

-- ---------------------------------------------------------------------------
-- Direct company outreach — foundation only (storage layer).
--
-- Kept in its own two tables rather than overloading `jobs`: an outreach
-- target is a company, not a posting, and reusing the jobs table would break
-- every existing dedupe/status assumption the job board relies on. Nothing
-- above this line changes.
--
-- Message status vocabulary, enforced in application code
-- (src/database/models.py OutreachStatus) like JobStatus, not by a CHECK
-- constraint, so it can grow without a migration:
--   DRAFT, APPROVED, SENT, FAILED, DO_NOT_CONTACT
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS outreach_companies (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    website               TEXT,
    contact_email         TEXT,                       -- the address outreach to this company goes to
    -- How the address above was obtained, and the page that proves it. Set by
    -- src/outreach/contact_discovery.py, which only ever records an address a
    -- company actually published (its own careers/contact page, or the text of
    -- a real ATS posting). An address is never constructed from a person's
    -- name, so these columns can always answer "why do we believe this is real?".
    contact_name          TEXT,                       -- named person, only when they published it themselves
    contact_source        TEXT,                       -- config | careers_page | job_posting
    contact_evidence_url  TEXT,                       -- the page the address was read from
    -- A recruiter/hiring-manager profile the company linked to from its own
    -- site. Stored for the user to act on BY HAND: nothing in this codebase
    -- sends a LinkedIn message, and this is never used to derive an email.
    linkedin_url          TEXT,
    -- An X/Twitter handle the company links to from its own pages, stored
    -- for the user to act on by hand exactly like linkedin_url is above.
    -- Nothing in this codebase messages via X, and this is never used to
    -- derive an email.
    x_handle              TEXT,
    x_name                TEXT,
    source                TEXT NOT NULL DEFAULT 'config',
    -- Which public ATS board to read this company's real postings from, using
    -- the existing adapters in src/job_discovery/sources/. Optional: when
    -- blank, the research layer falls back to matching config/employers.json
    -- by name. No board and no match simply means no postings were found.
    platform              TEXT,                       -- greenhouse | lever | smartrecruiters | ashby
    identifier            TEXT,                       -- that platform's board token/slug for this company
    -- Suppression lives on the company, not on a message: an opt-out has to
    -- block every future draft, not just the one that prompted it.
    do_not_contact        INTEGER NOT NULL DEFAULT 0,
    do_not_contact_reason TEXT,
    notes                 TEXT,
    -- Summary of the last research run (src/outreach/research.py): which
    -- board was read, how many real postings were found, their titles and
    -- the technologies they name. Stored so the dashboard can show what was
    -- actually found without re-fetching anyone's job board on page load.
    research_json         TEXT,
    researched_at         TEXT,
    -- Every published contact address src/outreach/contact_discovery.py
    -- found on this company's own pages or in their real postings, stored
    -- as a JSON array of {email, source, evidence_url} objects. The
    -- address the SENDER uses is still contact_email above — this column
    -- is the picker's inventory: everything discovered, so the user can
    -- see all options and switch to a different one when careers@ isn't
    -- what they want. Nothing here is ever constructed or guessed; every
    -- entry has an evidence_url pointing at the page it was read from.
    discovered_contacts_json TEXT,
    dedupe_hash           TEXT NOT NULL,              -- normalized domain, else normalized name
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (dedupe_hash)
);

CREATE INDEX IF NOT EXISTS idx_outreach_companies_dnc ON outreach_companies (do_not_contact);

CREATE TABLE IF NOT EXISTS outreach_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES outreach_companies (id) ON DELETE CASCADE,
    -- Snapshot of the address actually used, not a join to the company's
    -- current contact_email: that column can change later, and the audit
    -- question is always "where did this specific message go?".
    recipient_email TEXT,
    subject         TEXT,
    body            TEXT,
    -- The verified personalization analysis this specific email was written
    -- from (src/outreach/personalization.py), including what verification
    -- dropped. Lets the review UI show why the email says what it says.
    analysis_json   TEXT,
    -- The posting evidence this email was written from, trimmed to what the
    -- model actually saw. Stored on the message so the deterministic quality
    -- gate (src/outreach/quality_gate.py) can re-verify the email against it
    -- at any time — including after an edit — without a network call.
    research_snapshot_json TEXT,
    -- Result of the final deterministic eligibility gate. approve_message()
    -- refuses to promote a message unless gate_passed = 1, which is what
    -- makes "a failed message can never become sendable" structural rather
    -- than a convention callers have to remember.
    gate_passed     INTEGER,
    gate_reasons_json TEXT,
    status          TEXT NOT NULL DEFAULT 'DRAFT',
    send_attempts   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    approved_at     TEXT,
    sent_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_outreach_messages_status ON outreach_messages (status);
CREATE INDEX IF NOT EXISTS idx_outreach_messages_company_id ON outreach_messages (company_id);
CREATE INDEX IF NOT EXISTS idx_outreach_messages_sent_at ON outreach_messages (sent_at);
