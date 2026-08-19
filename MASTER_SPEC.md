# IT Job Hunter — Master Specification

This is the canonical specification for the IT Job Hunter project, as given
to the engineering assistant at project kickoff. It is preserved here
verbatim (lightly reformatted to Markdown) so any future session — human or
AI — has a durable, authoritative reference instead of relying on chat
history. If behavior described elsewhere in the codebase ever conflicts
with this document, treat this document as the source of truth unless the
user has explicitly changed a requirement (see "Deviations & decisions" at
the bottom for changes made during implementation, with rationale).

You are the lead engineer for a project called "IT Job Hunter".

BUILD this project, not merely explain how to build it.

## Non-negotiable constraints

- The entire system must cost $0/month.
- Do NOT use paid APIs.
- Do NOT use Claude/OpenAI/Gemini API.
- Claude Pro is for development assistance only, NOT for runtime API calls.
- Use local AI through Ollama.
- The system will primarily run on the user's Mac/laptop.
- It does NOT need to run 24/7.
- If the laptop is on, the automation can run in the background.
- If the laptop is off/asleep, nothing needs to run.
- Oracle Cloud is optional and should NOT be required.
- Do not introduce services that require credit cards or paid plans.
- Do not silently introduce cloud APIs with free trials that can later charge money.

---

## 1. Project goal

Build a local automated job-hunting assistant focused specifically on
helping the user break into the Australian IT job market.

The system should:

1. Discover suitable jobs.
2. Filter aggressively for ENTRY-LEVEL IT desk-based roles.
3. Reject mid-level/senior jobs.
4. Deduplicate jobs.
5. Analyze promising jobs using a local LLM.
6. Score each job based on fit.
7. Tailor the user's EXISTING resume to the job.
8. DO NOT rebuild the resume from scratch every time.
9. Modify/reorder relevant sections and bullets from the master resume.
10. Write a new tailored cover letter for each good job.
11. Run quality control to prevent fabricated experience/skills.
12. Generate clean PDF resume + cover letter.
13. Store everything locally.
14. Present the job with links.
15. Allow manual application when the application is external/manual.
16. Track applications and status.
17. Optionally automate form filling ONLY where technically/legally
    permitted and without bypassing CAPTCHA, anti-bot systems, login
    protections, or site restrictions.
18. Jobs that cannot safely/legitimately be automated must ALWAYS be placed
    in a manual application queue.

The system is an APPLICATION ASSISTANT, not a spam bot.

---

## 2. Target jobs

The primary goal is an entry-level IT desk job.

Prioritize:

IT Support Officer, IT Support Technician, IT Support, Service Desk
Analyst, Service Desk Officer, Help Desk Technician, Help Desk, Desktop
Support Technician, Desktop Support, Level 1 Support, Level 1 Service Desk,
L1 Support, Junior IT Support, Junior Systems Support, Junior Application
Support, Application Support, Technical Support Officer, Technical Support,
IT Technician, ICT Support Officer, ICT Support Technician, Graduate IT,
IT Graduate, Graduate Technology, Technology Graduate, Junior Technical
Analyst, Junior QA / Test Analyst, Junior Systems Administrator, Junior
Data Analyst, Junior Business Analyst, Technical Customer Support, SaaS
Technical Support, NOC Technician L1.

Prioritize locations: Sydney, Greater Sydney, NSW, Remote Australia, Hybrid
Sydney. Location preferences must be configurable.

---

## 3. Hard job rejection rules

Reject jobs containing or strongly indicating: Senior, Senior-level,
Mid-level, Lead, Team Lead, Manager, IT Manager, Principal, Architect,
Solutions Architect, Senior Engineer, Senior Analyst, L2, Level 2, L3,
Level 3, 3+ years required, 4+ years required, 5+ years required,
Extensive professional experience, Significant professional experience,
Management responsibilities.

Do not send these jobs to the expensive/local AI analysis stage if simple
rules can reject them. Use a cheap first-pass filter before invoking
Ollama.

---

## 4. Experience filter

Prefer: No experience required, Entry level, Junior, Graduate, Trainee,
0-1 years, 1 year, 1-2 years.

Jobs requiring 2 years can be considered if the rest of the job is highly
suitable. Jobs requiring 3+ years should normally be rejected. Configurable.

---

## 5. Job sources

Support job discovery from legitimate sources: SEEK, Indeed, other
legitimate Australian job sources, employer career pages, public job
feeds/APIs where available.

IMPORTANT: Do not bypass CAPTCHA, Cloudflare challenges, anti-bot systems,
authentication protections, rate limits, robots restrictions, or technical
access controls. Do not implement scraping that clearly violates a site's
terms.

Where direct automated collection isn't appropriate, provide a mechanism
for importing jobs manually: pasting job URLs, pasting job descriptions,
importing supported feeds. The architecture must allow additional job
sources later.

---

## 6. Application model

**TYPE A**: Application can safely/legitimately be assisted through an
accessible employer ATS/form.

**TYPE B**: Application requires manual interaction or external website.
Manual application queue must show: job title, company, location, job
source, job URL, match score, why it matches, missing skills, tailored
resume, cover letter, apply button/link, status. The user manually
completes submission. Do NOT bypass CAPTCHA or anti-bot systems.

---

## 7. Master resume system

Extremely important: the AI must NOT rebuild the entire resume from scratch
for every application.

A MASTER RESUME is stored locally in structured form
(`data/master_resume.json`):

```json
{
  "personal": {},
  "summary": "",
  "skills": [],
  "education": [],
  "experience": [],
  "projects": [],
  "certifications": [],
  "additional": []
}
```

Every important resume bullet has a stable ID, e.g.:

```json
{
  "id": "project_01_bullet_03",
  "text": "...",
  "skills": ["Python", "MySQL", "data analysis"]
}
```

The AI generates MODIFICATIONS to the master resume rather than inventing
an entirely new resume, e.g.:

```json
{
  "summary": {"action": "rewrite", "text": "..."},
  "skills": {"action": "reorder", "order": [...]},
  "projects": {"action": "reorder", "order": [...]},
  "bullet_changes": [...]
}
```

The renderer then applies those modifications. The master resume remains
unchanged.

---

## 8. Resume tailoring rules

The AI MAY: reorder skills, reorder projects, rewrite the summary, rewrite
existing bullets, emphasize relevant existing experience, change wording to
match job terminology, remove irrelevant bullets, select the most relevant
projects, adjust section ordering.

The AI MUST NOT: invent employment, invent companies, invent projects,
invent certifications, invent achievements, invent years of experience,
claim professional experience that does not exist, claim skills the user
does not possess, fabricate metrics, fabricate responsibilities.

If the job asks for a skill the user doesn't have: DO NOT pretend they have
it. Instead, omit it, mention adjacent transferable skills where
appropriate, or optionally flag it as a missing skill.

---

## 9. Cover letter

Unlike the resume, the cover letter is generated from scratch for each job.

Inputs: job title, company, job description, job analysis, master resume,
tailored resume, user's real skills, user's real education, relevant
projects, relevant experience.

The cover letter must: be concise, sound natural, avoid generic AI
language, avoid exaggerated enthusiasm, be specific to the role, mention
relevant skills, mention relevant projects/experience, explain why the
applicant is suitable, never fabricate anything.

Target approximately 250-400 words unless configured otherwise.

---

## 10. Local AI

Use Ollama. Do NOT use OpenAI API, Anthropic API, Google Gemini API, paid
APIs, or API trials. The AI runtime must be local. Create an AI abstraction
layer so the model can be changed later:

```
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=<configured-model>
```

Initially benchmark a small model suitable for the user's Mac. Do not
blindly assume a model. Create a setup script that detects the machine and
recommends a suitable model.

---

## 11. n8n

Use n8n as the workflow engine. Prefer Docker for reproducibility. Create
`docker-compose.yml`. Services initially: n8n, optionally supporting
services only when necessary. Ollama can run natively or through Docker
depending on what is most reliable for the user's Mac. Avoid unnecessary
containers. Do not use paid n8n cloud — local n8n only.

---

## 12. Database

Use SQLite initially. Do not introduce PostgreSQL unless genuinely
necessary.

Tables: `jobs`, `applications`, `job_sources`, `resume_versions`,
`cover_letters`, `settings`.

`jobs` contains at minimum: id, source, source_job_id, url, title, company,
location, salary, description, date_found, category,
experience_required, fit_score, status, created_at, updated_at.

`applications`: id, job_id, resume_path, cover_letter_path, status,
date_applied, notes, created_at, updated_at.

Statuses: NEW, ANALYZING, QUALIFIED, READY_TO_APPLY, APPLIED, REJECTED,
INTERVIEW, OFFER, SKIPPED.

---

## 13. Job pipeline

```
JOB SOURCE
    ↓
NORMALIZE
    ↓
DEDUPLICATE
    ↓
CHEAP FILTER
    ↓
ENTRY-LEVEL IT FILTER
    ↓
LOCAL AI ANALYSIS
    ↓
FIT SCORE
    ↓
QUALIFIED?
    ↓
RESUME TAILORING
    ↓
COVER LETTER
    ↓
QUALITY CONTROL
    ↓
PDF GENERATION
    ↓
APPLICATION QUEUE
```

---

## 14. Cheap filter

Implement deterministic filtering before AI. Positive keywords: IT
Support, Service Desk, Help Desk, Desktop Support, Technical Support, ICT
Support, Application Support, IT Technician, Junior IT, Graduate IT, L1,
Level 1. Negative keywords: Senior, Lead, Manager, Principal, Architect,
L2, L3, Level 2, Level 3, 3+ years, 5+ years. Configurable.

---

## 15. AI job analysis

Use Ollama to return STRICT JSON:

```json
{
  "category": "ENTRY_LEVEL_IT",
  "fit_score": 88,
  "experience_required": "0-1 years",
  "desk_based": true,
  "recommendation": "APPLY",
  "matched_skills": [],
  "missing_skills": [],
  "relevant_keywords": [],
  "reasons": [],
  "concerns": []
}
```

The workflow must validate the JSON. If invalid: retry, then fail safely.
Never assume AI output is valid.

---

## 16. Job scoring

Transparent score. Example weights: Entry-level suitability 30%, IT
relevance 25%, Skills match 20%, Experience match 15%, Location 10%.
Configurable weights. Do not let a high skills score override a hard
seniority rejection. Example: Senior job + 95% skill match = REJECT.

---

## 17. Resume tailoring pipeline

```
MASTER RESUME + JOB DESCRIPTION + JOB ANALYSIS -> TAILORING INSTRUCTIONS
MASTER RESUME + TAILORING INSTRUCTIONS -> TAILORED RESUME
```

The renderer generates HTML and PDF. The visual template must remain
consistent. Do not let the LLM arbitrarily redesign the resume.

---

## 18. Cover letter pipeline

Inputs: master profile, job, job analysis, tailored resume. Output:
`cover-letter.md`, `cover-letter.pdf`. Use a consistent professional
template.

---

## 19. Quality control

Before an application reaches READY_TO_APPLY, run a second local AI
validation. Check for: fabricated skills, fabricated experience, fabricated
achievements, wrong dates, wrong company, wrong job title, unsupported
claims, contradictions, missing required sections.

Return:

```json
{"passed": true, "issues": []}
```

If false: do not send to application queue; attempt correction; revalidate;
if still invalid, flag for manual review.

---

## 20. PDF generation

Use a reliable local renderer. Prefer HTML/CSS → Chromium/PDF or another
fully free local method. Resume PDF should be ATS-friendly. Avoid:
complicated graphics, excessive columns, text embedded in images, unusual
fonts, unnecessary icons, overly decorative designs.

---

## 21. Application dashboard

Simple local dashboard showing: NEW JOBS, QUALIFIED JOBS, READY TO APPLY,
APPLIED, INTERVIEW, REJECTED, SKIPPED. Each job card: title, company,
location, source, match score, experience, matched skills, missing skills.
Buttons: view job, open application, view resume, view cover letter, mark
applied, skip.

---

## 22. Notifications

Free/local notifications where practical: desktop notification, Telegram
bot (optional). Do not require a paid notification service.

---

## 23. Application folder structure

```
job-hunter/
├── docker-compose.yml
├── .env.example
├── README.md
│
├── data/
│   ├── jobs.db
│   ├── master_resume.json
│   └── settings.json
│
├── applications/
│   └── company-role/
│       ├── job.json
│       ├── resume.pdf
│       ├── cover-letter.pdf
│       └── cover-letter.md
│
├── templates/
│   ├── resume.html
│   └── cover-letter.html
│
├── scripts/
│   ├── setup.sh
│   ├── backup.sh
│   └── healthcheck.sh
│
├── src/
│   ├── job_filter/
│   ├── ai/
│   ├── resume/
│   ├── cover_letter/
│   ├── pdf/
│   └── database/
│
└── workflows/
    ├── job-discovery.json
    ├── job-analysis.json
    ├── application-generation.json
    └── application-tracking.json
```

Adjust the structure if a better-justified architecture exists (see
"Deviations & decisions" below for what actually changed and why).

---

## 24. Configuration

`.env.example`:

```
APP_NAME=IT-Job-Hunter

AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=

TARGET_DOMAIN=IT
TARGET_LEVEL=ENTRY
TARGET_LOCATION=Sydney

MAX_EXPERIENCE_YEARS=2

MIN_FIT_SCORE=75

AUTO_GENERATE_APPLICATIONS=true
AUTO_SUBMIT_APPLICATIONS=false

ALLOW_BROWSER_AUTOMATION=false
```

All dangerous/irreversible automation defaults to FALSE.

---

## 25. Security

Do not expose local n8n publicly. Use authentication. Do not commit
credentials, cookies, session tokens, API keys, SSH keys, personal secrets.
Create `.gitignore`. Protect personal resume information.

---

## 26. Cost protection

This project must remain $0. Build explicit protections against accidental
paid services. No: paid APIs, API credit cards, cloud AI, paid databases,
paid hosting, paid email services, paid automation services. If a feature
normally requires a paid service, STOP and ask before implementing it.
Prefer local alternatives.

---

## 27. Job source safety

Never implement: CAPTCHA solving, CAPTCHA bypass, anti-bot bypass,
Cloudflare bypass, fingerprint spoofing, stealth browser automation
intended to evade detection, account takeover, credential harvesting, mass
spam submission. Manual application is the fallback.

---

## 28. Browser automation

Playwright may be used later. `AUTO_SUBMIT_APPLICATIONS=false` by default.
Browser automation should stop before final submission unless explicitly
configured otherwise AND the target site's policies permit it. For
unsupported sites: create a manual application task.

---

## 29. Testing

Build tests for: job filtering, seniority rejection, experience filtering,
duplicate detection, JSON parsing, resume modification, hallucination
detection, scoring, database operations, PDF generation.

Sample jobs (used verbatim in the test suite):

1. Entry-level IT Support Officer
2. Senior Systems Engineer
3. Graduate IT Support
4. Service Desk Analyst requiring 1 year
5. IT Manager
6. Junior Application Support
7. Software Engineer requiring 5 years

Expected filtering should be deterministic.

---

## 30. Observability

Useful logs, e.g.:

```
[JOB DISCOVERY] Found: 42
[FILTER] Rejected senior: 14 / Rejected non-IT: 11 / Duplicates: 5 / Passed: 12
[AI] Analyzed: 12 / Qualified: 7
[APPLICATION] Generated: 7 / Flagged for review: 1
```

Never log private credentials.

---

## 31. Backup

Local backup script backing up: database, master resume, application
files, settings, workflows. Do not back up secrets unnecessarily.

---

## 32. Setup experience

Beginner-friendly `setup.sh` that: checks OS, checks Docker, checks
Ollama, checks Python, creates directories, creates `.env`, starts n8n,
verifies n8n, verifies Ollama, pulls/selects the recommended model,
initializes SQLite, runs tests, prints the local URLs. Do not hide errors.

---

## 33. Claude Code workflow

Inspect existing files before major changes. Explain the architecture
briefly. Create a plan. Implement one logical stage at a time. Run tests
after each stage. Fix errors. Do not rewrite working code unnecessarily.
Do NOT dump an enormous amount of code into one file — use modular
architecture.

---

## 34. Build order

Phase 1: Project skeleton
Phase 2: Docker/n8n setup
Phase 3: Ollama integration
Phase 4: SQLite database
Phase 5: Master resume system
Phase 6: Job normalization
Phase 7: Cheap deterministic filtering
Phase 8: AI job analysis
Phase 9: Job scoring
Phase 10: Resume tailoring
Phase 11: Cover letter generation
Phase 12: Quality control
Phase 13: PDF generation
Phase 14: Application dashboard/queue
Phase 15: Notifications
Phase 16: Optional Playwright/manual-assist workflow
Phase 17: Backup and reliability

---

## 35. User profile (source of truth: `data/master_resume.json`)

Designed around an entry-level IT candidate. Known skills/profile to
incorporate ONLY where accurate: cybersecurity student, Python, Java,
React, MySQL, Excel, full-stack development projects, data analytics
coursework, data science/ML coursework. Do not invent professional
experience. The master resume is the ultimate source of truth — if
information is missing from it, do not fabricate it.

---

## 36. User experience (target)

Laptop turns on → n8n runs → system finds jobs → filters out
irrelevant/senior positions → shortlist presented, e.g.:

```
🔥 94 — IT Support Officer
Company: Example
Location: Sydney
Experience: 0-1 years

✓ Entry-level
✓ Desk-based
✓ Python relevant
✓ Troubleshooting
✓ Good skills match

Resume: READY
Cover letter: READY

[OPEN JOB]
```

User manually applies, then [MARK AS APPLIED]. System tracks everything.

---

## 37. Design principle

NOT an autonomous spam application bot.

```
DISCOVER → FILTER → ANALYZE → PERSONALIZE → PREPARE → PRESENT → HUMAN REVIEWS → HUMAN SUBMITS
```

Automate repetitive preparation, not human judgment.

---

## 38. First task (completed)

Inspect the environment, report OS/Docker/Ollama/Python/n8n status and
existing files, recommend an architecture, propose a plan, and wait for
approval before building. (Done at project kickoff — see git history from
commit `bf6810c` onward.)

---

## Deviations & decisions

Changes made during implementation, and why, superseding the letter of the
spec above where they conflict:

- **n8n hosting**: Docker Desktop (user's explicit choice over native npm)
  — installed via Homebrew, `docker-compose.yml` binds n8n to `127.0.0.1`
  only, and auth is via n8n's own built-in owner-account system (the
  `N8N_BASIC_AUTH_*` env vars in `.env.example` are inert leftovers for
  older n8n versions; modern n8n replaced them).
- **PDF engine**: Playwright + headless Chromium (user's explicit choice),
  doubles as the Phase 16 browser-automation engine.
- **Default AI models** (chosen via `python -m src.ai.benchmark`, not
  assumed): `OLLAMA_MODEL=llama3:latest` for job analysis, resume
  tailoring, and cover letter generation — reliable strict-JSON output,
  ~9.6s/call. `qwen3:8b`'s "thinking" mode made it too slow (87-120s/call).
  `OLLAMA_QC_MODEL=mistral:latest` — a **different** model than the rest of
  the pipeline, because live testing showed `llama3:latest` confidently
  hallucinates false fabrication claims on the QC cross-document
  verification task specifically (e.g. claiming a real skill "isn't
  supported by the master resume"), while `mistral:latest` passed the same
  real scenarios cleanly. See `src/quality_control/qc.py` module docstring.
- **QC is two layers, not one AI call**: a deterministic layer (reuses the
  fabrication_guard heuristics from tailoring/cover-letter, zero
  hallucination risk) handles skill/term/number fabrication, required
  sections, and date/company/title consistency; the AI layer is narrowed to
  only unsupported claims and resume-vs-cover-letter contradictions — the
  two categories that genuinely need semantic judgment. This exists because
  the AI-only approach proved unreliable in live testing (see commit
  `26419f6`).
- **Job sources**: automated SEEK/Indeed scraping is NOT implemented as a
  first-class source, since both sit behind anti-bot protections the spec
  explicitly forbids bypassing. The reliable, always-available Type B
  intake path is manual paste import (`src/sources/manual_import.py`),
  with the `JobSourceKind` abstraction left open for a genuine public
  feed/API later.
- **Extra top-level packages** beyond the spec's listed
  `job_filter/ai/resume/cover_letter/pdf/database`: `src/sources/` (job
  import abstraction) and `src/quality_control/` (kept separate from `ai/`
  specifically to avoid a circular import — `resume`/`cover_letter` import
  from `ai`, and QC needs to import from both `ai` and `resume`/
  `cover_letter`, so it sits above both in the dependency graph).
- **Skills tailoring**: originally implemented as strict reorder-only (no
  removal), matching the spec's literal "reorder skills" (vs. projects'
  explicit "select most relevant"). Relaxed to allow subset selection
  after live testing showed models consistently and reasonably want to
  omit clearly-irrelevant skills (e.g. creative-tool skills on a technical
  support resume) — removing a *true* skill from display is not a
  fabrication risk (only inventing one is), and fighting this natural,
  sensible behavior caused repeated hard failures. Full fabrication
  protection (rejecting any invented skill) is unchanged.
