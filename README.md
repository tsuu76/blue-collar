# IT Job Hunter

A local, **$0/month** automated assistant for finding and preparing applications for
entry-level IT desk jobs in Australia (Sydney/NSW-focused, configurable). It discovers
and filters jobs, tailors your existing resume, drafts cover letters, runs a
hallucination check, and hands you a ready-to-review application — **you always click
submit.**

See [MASTER_SPEC.md](MASTER_SPEC.md) for the full original specification, including a
"Deviations & decisions" section documenting every implementation choice that differs
from the letter of the spec and why.

## Design principle

```
DISCOVER → FILTER → ANALYZE → PERSONALIZE → PREPARE → PRESENT → HUMAN REVIEWS → HUMAN SUBMITS
```

This is an application *assistant*, not a spam bot. Nothing is submitted automatically —
every job lands in a manual review queue with a generated resume/cover letter for you to
check and send yourself. No CAPTCHA/anti-bot bypass, no auto-fill, no auto-submit are
implemented anywhere in this codebase.

## Cost protection

This project must never cost money. Concretely:

- **AI**: Ollama only, running locally. No OpenAI/Anthropic/Gemini API keys anywhere in
  this codebase — the AI abstraction layer (`src/ai/`) only implements a local provider.
  Two models are used, chosen by benchmarking (`python -m src.ai.benchmark`), not
  assumed: `llama3:latest` for analysis/tailoring/cover letters, `mistral:latest`
  specifically for quality control (see `src/quality_control/qc.py` for why).
- **Workflow engine**: n8n running locally via Docker Desktop (free for personal use, no
  credit card), bound to `127.0.0.1` only. Never uses n8n cloud.
- **Database**: SQLite, a local file, no hosted database.
- **PDF rendering**: Playwright + local headless Chromium — no paid rendering API.
- **Job sources**: manual paste import (`src/sources/`) — automated SEEK/Indeed scraping
  is not implemented since both sit behind anti-bot protections this project doesn't
  bypass, per spec.
- **Dashboard**: local Flask app, bound to `127.0.0.1` only.
- **Notifications**: macOS local notifications by default. Telegram is optional and free
  (you supply your own bot token) — never required.

If any future feature would normally require a paid service, the assistant will stop
and ask before implementing it, per this project's operating rules.

## Requirements

- macOS (Apple Silicon or Intel)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free, no card required for personal use) — optional, only needed for n8n
- [Ollama](https://ollama.com) installed and running locally
- Python 3.10+

## Quick start

```bash
./scripts/setup.sh
```

This checks your environment, creates `.env` and a Python venv, installs dependencies
(including Playwright's Chromium — a one-time ~150-180MB download), starts n8n if Docker
is available, verifies Ollama, initializes the database, runs the full test suite, and
prints the local URLs.

Then, before anything else, populate `data/master_resume.json` with your **real** resume
content — see `templates/master_resume.example.json` for the structure. Nothing in this
system will fabricate resume content; if it's not in that file, it can't appear in a
tailored resume or cover letter.

```bash
source .venv/bin/activate
python -m src.dashboard.app
```

Visit `http://localhost:8420`, use **Import job** to paste in a job you found (title,
company, description, URL), then open it and click **Process this job** to run it
through the full pipeline (filter → AI analysis → scoring → tailoring → cover letter →
quality control → PDF). Review the result, and apply yourself when you're ready.

## Everyday use

- `scripts/healthcheck.sh` — verify Ollama/Docker/n8n/database/dashboard are all in a
  good state, any time.
- `scripts/backup.sh` — back up the database, master resume, and generated applications
  to `backups/` (never includes `.env` or other secrets).
- `python -m src.pipeline` isn't a CLI entry point on its own — process jobs either from
  the dashboard's **Process this job** button, or in a Python shell:
  `from src.pipeline import process_new_jobs; process_new_jobs()` to run every `NEW` job
  in one batch.

## Project structure

```
job-hunt/
├── MASTER_SPEC.md          # the original spec, preserved verbatim
├── docker-compose.yml      # n8n, local only, bound to 127.0.0.1
├── .env.example            # copy to .env and configure (setup.sh does this for you)
├── data/                   # jobs.db, master_resume.json (gitignored — personal data)
├── applications/           # generated resume/cover-letter PDFs per application (gitignored)
├── backups/                # scripts/backup.sh output (gitignored)
├── templates/               # HTML templates for resume + cover letter PDFs (ATS-friendly)
├── scripts/                 # setup.sh, backup.sh, healthcheck.sh
├── src/
│   ├── job_filter/          # deterministic cheap filter + AI-informed scoring
│   ├── ai/                  # Ollama abstraction layer, job analysis, JSON schemas
│   ├── resume/               # master resume schema + diff-based tailoring engine
│   ├── cover_letter/         # from-scratch-per-job cover letter generation
│   ├── quality_control/      # two-layer QC: deterministic + narrowed AI pass
│   ├── pdf/                  # Jinja2 + Playwright PDF rendering
│   ├── database/              # SQLite schema + repositories
│   ├── sources/                # manual job import (paste URL/description)
│   ├── browser_assist/         # application-type classification, link reachability
│   ├── notifications/           # macOS desktop + optional Telegram
│   ├── pipeline/                 # orchestrates every phase into one flow
│   └── dashboard/                 # local Flask UI (Kanban board, job detail, import)
├── tests/                    # 217 tests across every module
└── workflows/                 # n8n workflow exports (JSON) — optional
```

## Master resume

Your real resume lives in `data/master_resume.json` as structured, ID-tagged content.
The AI is only ever allowed to **reorder, select a subset of, or rewrite the wording
of** existing entries — it cannot invent employment, projects, certifications, or
skills that aren't already in this file. Every generated resume/cover letter is checked
against this file by a fabrication guard (heuristic, on every generation) and a
dedicated quality-control pass (a second, independent AI check) before it's ever shown
to you as ready to apply.

## Status

All 17 build phases complete. 217 tests passing. See git history for a phase-by-phase
log, and `MASTER_SPEC.md`'s "Deviations & decisions" section for what changed from the
original spec and why.
