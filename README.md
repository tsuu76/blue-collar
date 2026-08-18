# IT Job Hunter

A local, **$0/month** automated assistant for finding and preparing applications for
entry-level IT desk jobs in Australia (Sydney/NSW-focused, configurable). It discovers
and filters jobs, tailors your existing resume, drafts cover letters, runs a
hallucination check, and hands you a ready-to-review application — **you always click
submit.**

## Design principle

```
DISCOVER → FILTER → ANALYZE → PERSONALIZE → PREPARE → PRESENT → HUMAN REVIEWS → HUMAN SUBMITS
```

This is an application *assistant*, not a spam bot. Nothing is submitted automatically
unless you explicitly opt in (`AUTO_SUBMIT_APPLICATIONS=true`), and even then, browser
automation stops before any final submit button, CAPTCHA, or anti-bot challenge — those
jobs always fall through to the manual application queue.

## Cost protection

This project must never cost money. Concretely:

- **AI**: Ollama only, running locally. No OpenAI/Anthropic/Gemini API keys anywhere in
  this codebase — the AI abstraction layer (`src/ai/`) only implements a local provider.
- **Workflow engine**: n8n running locally via Docker Desktop (free for personal use, no
  credit card). Never uses n8n cloud.
- **Database**: SQLite, a local file, no hosted database.
- **PDF rendering**: Playwright + local headless Chromium — no paid rendering API.
- **Job sources**: manual paste import + optional public feeds. No paid job-board APIs.
- **Notifications**: macOS local notifications by default. Telegram is optional and free
  (you supply your own bot token) — never required.

If any future feature would normally require a paid service, the assistant will stop
and ask before implementing it, per this project's operating rules.

## Requirements

- macOS (Apple Silicon or Intel)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free, no login card required for personal use)
- [Ollama](https://ollama.com) installed and running locally
- Python 3.10+

## Quick start

```bash
cp .env.example .env
./scripts/setup.sh
```

`setup.sh` checks your environment, starts n8n, verifies Ollama, initializes the
database, runs the test suite, and prints the local dashboard/n8n URLs.

## Project structure

```
job-hunt/
├── docker-compose.yml     # n8n, local only
├── .env.example           # copy to .env and configure
├── data/                  # jobs.db, master_resume.json, settings.json (gitignored)
├── applications/          # generated resume/cover-letter PDFs per application (gitignored)
├── templates/             # HTML templates for resume + cover letter PDFs
├── scripts/               # setup.sh, backup.sh, healthcheck.sh
├── src/
│   ├── job_filter/        # deterministic + AI filtering
│   ├── ai/                 # Ollama abstraction layer
│   ├── resume/             # master resume + tailoring engine
│   ├── cover_letter/       # cover letter generation
│   ├── pdf/                 # Playwright-based PDF rendering
│   ├── database/            # SQLite schema + access
│   └── dashboard/            # local read-only web UI
├── tests/
└── workflows/               # n8n workflow exports (JSON)
```

## Master resume

Your real resume lives in `data/master_resume.json` as structured, ID-tagged content
(see `docs/master_resume_schema.md` once Phase 5 lands). The AI is only ever allowed to
**reorder, rewrite wording of, or omit** existing entries — it cannot invent employment,
projects, certifications, or skills that aren't already in this file. Cover letters are
generated fresh per job but are grounded exclusively in the same file.

## Status

Build in progress — see the phase checklist in project notes. Each phase is implemented
and tested before the next begins.
