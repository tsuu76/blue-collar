# Employer registry (`employers.json`)

Each entry tells the discovery layer (`src/job_discovery/`) which employer to
check and which adapter to use. Only four platforms are supported, because
only these four have genuinely public, officially-documented, unauthenticated
job-board APIs — see each adapter's module docstring in
`src/job_discovery/sources/` for the specific evidence. Nothing here scrapes
rendered HTML or bypasses any restriction.

```json
{
  "company": "SafetyCulture",
  "platform": "ashby",
  "identifier": "safetyculture",
  "enabled": true
}
```

- `platform`: one of `greenhouse`, `lever`, `smartrecruiters`, `ashby`.
- `identifier`: the platform-specific board token/company slug — see below
  for how to find it.
- `enabled`: set `false` to keep an entry in the file without checking it.

## Finding a company's identifier

Visit the company's careers page and look at where "Apply" links actually
send you, or check the URL of their jobs listing page itself:

| Platform | URL pattern | `identifier` is... |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/COMPANY` or `job-boards.greenhouse.io/COMPANY` | `COMPANY` |
| Lever | `jobs.lever.co/COMPANY` | `COMPANY` |
| SmartRecruiters | `jobs.smartrecruiters.com/COMPANY` | `COMPANY` (case-sensitive) |
| Ashby | `jobs.ashbyhq.com/COMPANY` | `COMPANY` |

If a company's careers page doesn't match any of these four patterns (most
large enterprises — banks, telcos, most Workday/SuccessFactors/Taleo
installs — don't), there's no safe automated way to discover their postings
today. Leave them out of this file; you can still paste individual job URLs
into the dashboard's **Import job** form at any time, exactly as before.

## Before adding a new entry

Sanity-check the identifier actually resolves before enabling it:

```bash
curl -s "https://boards-api.greenhouse.io/v1/boards/COMPANY/jobs" | head -c 200
curl -s "https://api.lever.co/v0/postings/COMPANY?mode=json" | head -c 200
curl -s "https://api.smartrecruiters.com/v1/companies/COMPANY/postings" | head -c 200
curl -s "https://api.ashbyhq.com/posting-api/job-board/COMPANY" | head -c 200
```

A 404 or an empty/error response means that's not the right identifier (or
that company isn't on that platform) — don't enable an entry that 404s, it
will just fail silently on every discovery run.
