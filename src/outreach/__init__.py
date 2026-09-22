"""
Direct company outreach — the second pathway alongside job applications.

One automatic run takes a company from the registry all the way to a sent
email, in layers that each stand on their own:

  - src/database/outreach_repo.py    — storage (companies, messages, status)
  - src/outreach/targets.py          — which real companies to consider
  - src/outreach/research.py         — their real public postings
  - src/outreach/contact_discovery.py— a published address, or none at all
  - src/outreach/personalization.py  — overlap, verified against evidence
  - src/outreach/email_draft.py      — the email, grounded in the resume
  - src/outreach/quality_gate.py     — the deterministic PASS/FAIL
  - src/outreach/sender.py           — the only SMTP code in the project
  - src/outreach/pipeline.py         — the orchestrator that runs all of it

Nothing is invented at any step: companies come from a file the user
maintains, postings come from public ATS APIs, contact addresses are only
used when a company published them, and the email may only say things the
postings and the master resume support.
"""
