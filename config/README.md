Two config-driven registries live here:

- **`employers.json`** — companies whose postings get discovered and turned
  into job applications (the original pathway).
- **`outreach_companies.json`** — companies to contact directly, whether or
  not they have a suitable posting right now. See
  [Outreach registry](#outreach-registry-outreach_companiesjson).

---

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

- `platform`: one of `greenhouse`, `lever`, `smartrecruiters`, `ashby`,
  `workday`, or `careers_page`.
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
| Workday | `TENANT.wdN.myworkdayjobs.com/[en-US/]SITE` | the whole URL, as `https://TENANT.wdN.myworkdayjobs.com/TENANT/SITE` |

Workday is the odd one out: its identifier is a URL rather than a slug,
because a Workday board is addressed by three things (tenant, cluster
number, career-site name) and none of them can be derived from a company
name. You never have to assemble it by hand — the careers resolver below
extracts it from the company's own site — but if you are adding one
manually, paste the career-site URL from your browser's address bar and
discovery will parse it.

If a company's careers page doesn't match any of these patterns, discovery
falls back to `careers_page`: a verified careers URL whose job data this
project cannot machine-read. Those entries are real and worth keeping —
they just need checking by hand. You can also paste individual job URLs
into the dashboard's **Import job** form at any time, exactly as before.

## The careers resolver

Most large Australian employers publish real openings that sit at no
guessable URL: the homepage links to a careers page, which links to a
hosted ATS, which is where the jobs actually live. `src/job_discovery/
careers_resolver.py` walks exactly that path, within a fixed budget of
about six requests per company:

```
homepage -> "Careers"/"Work with us" link -> careers.company.com
         -> ATS fingerprint -> tenant/site identifier -> public job endpoint
```

Three rules make this safe to point at real employers:

- **Nothing is derived from a company name.** An ATS tenant, board slug or
  endpoint URL is only ever read out of a URL that genuinely appeared in
  that company's own HTML — an anchor, a script `src`, an iframe, or a URL
  literal in inline JavaScript. A site that never mentions Workday can
  never be given a Workday identifier.
- **Every request is robots.txt-checked and rate-limited**, through the same
  `polite_get`/`polite_post` the rest of discovery uses.
- **Every result records its evidence.** The `notes` field of a registry
  entry names the page the identifier came from, so any entry can be traced
  back rather than taken on trust.

Platforms the resolver recognises but deliberately does *not* query —
PageUp, SuccessFactors, Taleo, JobAdder, iCIMS, Cornerstone and others — are
named in the entry's notes so you know what to check by hand. They stay
`careers_page` until someone verifies a real public endpoint for them; a
recognised vendor is not the same thing as a readable one.

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

---

# Outreach registry (`outreach_companies.json`)

Companies to reach out to directly. This is the **only** place a company
becomes an outreach target — nothing in the codebase invents, guesses, or
search-scrapes companies into this list, because doing that reliably would
need a paid company-data API or scraping sites whose terms forbid it.

You add the company; everything after that is automatic — reading their real
current postings, working out what they repeatedly hire for, checking that
against your actual resume, and drafting the email.

```json
{
  "company": "Example Pty Ltd",
  "website": "https://example.com.au",
  "platform": "greenhouse",
  "identifier": "examplecompany",
  "contact_email": "careers@example.com.au",
  "notes": "Sydney MSP, saw their grad program mentioned at uni",
  "enabled": true
}
```

| Field | Required | What it does |
|---|---|---|
| `company` | yes | Display name. |
| `website` | recommended | The dedupe key, so the same company can't be queued twice under two spellings. |
| `platform` + `identifier` | **effectively yes** | One of the four ATS platforms in `employers.json` (`identifier` = board token), or `careers_page` (`identifier` = the specific careers URL `src/outreach/discovery.py` verified — see below). Usually filled in by discovery, not typed by hand. Without one, there are no real postings to read, so the company is skipped. |
| `contact_email` | optional | An address you have verified yourself. Leave it blank and the run looks for one on the company's own careers/contact pages and in their postings — it never guesses, so if they publish nothing the company shows as `NEEDS_CONTACT`. An address you set here always wins and is never overwritten. |
| `notes` | optional | Your own note. Never sent, never used as evidence. |
| `enabled` | optional | `false` keeps the entry without contacting them. Defaults `true`. |

## Filling this file automatically (`candidate_companies.json`)

Rather than researching board identifiers by hand, list company **names** in
`candidate_companies.json` and let discovery verify them:

```bash
.venv/bin/python -m src.outreach.discovery          # dry run, shows what it found
.venv/bin/python -m src.outreach.discovery --write  # append verified companies
```

Each entry is a bare name, or an object:

```json
{"name": "Example Pty Ltd", "website": "https://example.com.au"}
```

or, if you already know its ATS board:

```json
{"name": "Example Pty Ltd", "platform": "greenhouse", "identifier": "examplecompany"}
```

Two independent ways a candidate gets verified — a company only needs one:

1. **ATS guess.** Up to three slug guesses probed against Greenhouse, Lever,
   Ashby and SmartRecruiters. A guess that 404s or returns nothing is
   discarded, never written.
2. **Careers-page probe.** When a real `website` is given, its own
   careers/jobs page is read directly, looking for schema.org `JobPosting`
   structured data — the same machine-readable markup companies publish for
   Google for Jobs. **This is what makes a company on Workday, PageUp,
   SuccessFactors, Taleo, JobAdder, or a fully custom careers site
   discoverable** — none of those platforms are required, and none of them
   need a bespoke adapter.

`website` is never guessed — supply a domain you (or Claude, via real web
search) have actually verified. The probe then only tries a short list of
CONVENTIONAL PATHS on that known-real domain (`/careers`, `/jobs`, …) and
only keeps what a real response there actually contains.

**A company with no matching structured data is not the same as a company
with no jobs.** When the careers page is reachable but has no `JobPosting`
markup — a Workday widget, a plain HTML list, an authentication-walled
portal — the company is still kept as a verified candidate, with its job
data marked unavailable rather than "confirmed zero". Only an authoritative
ATS API genuinely returning nothing counts as a confirmed zero.

**Nothing is discarded for having no current match**, either. Every verified
company is written to `outreach_companies.json`, ranked into one of four
priority tiers so you can see what's worth acting on first:

| Tier | Meaning |
|---|---|
| 1 | A current opening that clears the entry-level IT/cyber/software filter |
| 2 | A current graduate/junior/intern-shaped opening in tech |
| 3 | Any other current opening naming a recognised technology skill |
| 4 | No current match — either confirmed-empty, or job data unavailable. Kept for a later run to notice if that changes. |

Pass `--relevant-only` to narrow a run to tier 1 only; the default keeps
every tier.

Entries already in `outreach_companies.json` are never modified, so a
`contact_email` or identifier you set by hand survives every run.

Discovered companies arrive with **no contact email** (a company verified via
an ATS board also arrives with no website). Adding the website is worth doing
— it's what lets contact discovery read the company's own careers/contact
pages. Neither website nor contact email is ever invented; a company that
publishes no address anywhere shows as `NEEDS_CONTACT` until you add one.

Every request — ATS or careers-page — is robots.txt-checked before it is
made, and goes through the same rate-limited, identified fetcher every
adapter uses. Nothing here ever reads LinkedIn, and the run is slow by
design: a full candidate list can take several minutes.

### Growing the candidate pool

No code here invents a company. Getting to a large, continuously-replenished
pool (the project's target is 500+ legitimate Australian employers) is a
research workflow, not a one-time script: real company names and domains,
found through legitimate web search (industry "top employer" lists, ASX-listed
tech company registries, industry association directories — never scraped
from LinkedIn, which prohibits it), get appended to `candidate_companies.json`
in batches, and a discovery run verifies only what it can actually confirm.
A company that fails verification simply stays unverified; it is never
written on the strength of the candidate list alone.

## Running discovery

```bash
.venv/bin/python -m src.outreach.discovery --refresh
```

Dry run by default — it reports what it found and writes nothing. Add
`--write` to save. `--refresh` adds a second pass over companies already
registered as `careers_page`, re-resolving each one and upgrading any whose
real ATS the resolver can now find; `--refresh-only` runs just that pass.

The refresh is the only thing in this project that modifies an existing
registry entry. It rewrites `platform`, `identifier` and `notes` and
nothing else — **`contact_email` and `enabled` are always carried across
untouched**, so an address you found by hand and a company you deliberately
switched off both survive any number of runs. A company whose site is
unreachable on the day is reported and left exactly as it was, never
downgraded or removed.

## Running it

```bash
.venv/bin/python -m src.outreach.pipeline --dry-run
```

Drop `--dry-run` to let the run act. A run goes all the way through on its
own — research, contact discovery, writing, the quality gate, then sending —
with no approval step.

Two things control whether anything is actually transmitted:

- **`OUTREACH_DRY_RUN`** (`.env`). While it is `true` — the default — every
  step runs and the email is written and gated, but no SMTP connection is
  ever opened. It is an absolute veto: no flag, button or argument overrides
  it. Set it to `false` when you are happy with what it writes.
- **`OUTREACH_DAILY_LIMIT`** (`.env`, default `5`). The most emails that may
  go out in a day, counted across every run.

An email written while sending was off is not lost. The next run that *is*
allowed to send delivers that same message rather than writing a new one.

### How it finds someone to email

In priority order, using only what a company published itself:

1. `contact_email` in this file, or one you typed in the dashboard.
2. A `mailto:` link or address on the company's **own** careers/contact page.
3. An address written into the text of a real ATS posting.
4. A recruiter/hiring-manager LinkedIn profile the company linked to — stored
   and shown on the dashboard **for you to contact by hand**. Nothing in this
   project sends a LinkedIn message.

An address is never built from a person's name, never guessed from a pattern,
and never taken from a third-party site. No verified address means the company
is skipped.

## When a company is skipped

The pipeline reports a reason for every company rather than contacting one it
knows nothing about:

| Skip | Meaning |
|---|---|
| `SKIPPED_DO_NOT_CONTACT` | They opted out. Permanent. |
| `SKIPPED_ALREADY_CONTACTED` | An email has already gone to them. |
| `SKIPPED_PENDING_MESSAGE` | An email for them was already written and is waiting to go out. It sends on the next run that is allowed to send; a gate-failed one stays put until you edit or reject it. |
| `SKIPPED_NO_POSTINGS` | No ATS board configured, or their board returned nothing. Without real postings there is nothing specific to say. |
| `SKIPPED_NO_CONTACT_EMAIL` | No published address was found anywhere. Research is still saved, so the dashboard shows what they're hiring for and any LinkedIn profile found, and you can add an address by hand. |
| `SKIPPED_NO_OVERLAP` | Nothing they ask for matches your resume. There is no honest email to write. |
| `SKIPPED_DAILY_LIMIT` | The day's budget (`OUTREACH_DAILY_LIMIT`) is used up. |
| `SKIPPED_SEND_BLOCKED` | A pre-send re-check refused it at the last moment (they opted out, the address changed). Nothing was transmitted. |
| `FAILED_GATE` | The email did not pass the deterministic quality gate, so it can never be sent. It stays readable and editable on the dashboard. |
| `FAILED_SEND` | The mail server rejected it or the connection failed. Recorded on the message; the rest of the batch continues. |
