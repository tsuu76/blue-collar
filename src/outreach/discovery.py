"""
Automated company discovery — turn a list of candidate company NAMES (and,
where known, their real domains) into verified outreach targets, so
config/outreach_companies.json doesn't have to be maintained entirely by
hand.

Two independent ways a candidate gets verified, tried in order:

  1. ATS guess. The same approach this module has always used: guess a
     handful of sensible board slugs and *prove or discard* each guess by
     asking one of four platforms' own public job-board API
     (src/job_discovery/sources/greenhouse.py etc.). A guess that 404s or
     returns nothing is thrown away.

  2. Careers-page probe. NEW, and the reason this module no longer needs a
     company to be on one of those four platforms. When a candidate carries
     a real `website` (never guessed — see below), its own careers/jobs
     page is read directly (src/job_discovery/sources/generic_careers.py)
     looking for schema.org JobPosting structured data. This is what makes
     a company on Workday, PageUp, SuccessFactors, Taleo, JobAdder, or a
     fully custom careers site discoverable, without this project writing a
     bespoke adapter for each of those platforms' undocumented internals.

Neither path invents anything. That's the whole safety story here:

  - Company names (and website hints) in config/candidate_companies.json
    are candidates, not claims. Being listed there asserts nothing.
  - platform/identifier hints are verified live before use — a hint only
    saves a request, it is never trusted outright.
  - A website is only ever one you (or Claude, via real web research)
    supplied — this module never guesses a domain from a company name, and
    never fabricates a careers-page URL; it only tries a short list of
    CONVENTIONAL PATHS (/careers, /jobs, ...) on a domain already believed
    real, and only keeps what a real response on that domain actually says.
  - When a careers page is reachable but has no structured job data, the
    company is still kept as a legitimate, verified candidate — its job
    data is marked unavailable, never reported as "confirmed zero". Only an
    authoritative ATS API returning an empty result means a genuinely
    confirmed zero.
  - A company with no CURRENT matching opening is never discarded either.
    It is kept, ranked in the lowest priority tier, for the existing
    outreach pipeline to notice automatically if that changes on a later
    run — see priority_tier below.

No paid API, no search engine, no company-data service, no scraping of a
site that disallows it (every request is robots.txt-checked) and never
LinkedIn. Every HTTP request goes through job_discovery.base.polite_get (via
an adapter or the careers-page prober), so the existing per-host rate
limiting and identifying User-Agent apply everywhere.

Run it with:  python -m src.outreach.discovery [--write] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping

from src.config import PROJECT_ROOT
from src.database.models import normalize_for_dedupe
from src.job_discovery.base import JobDiscoverySource, polite_get
from src.job_discovery.registry import ADAPTERS
from src.job_discovery.sources.generic_careers import probe_careers_page
from src.job_filter.cheap_filter import run_cheap_filter
from src.outreach.research import extract_skills
from src.sources.base import NormalizedJob

from .targets import OUTREACH_CONFIG_PATH

logger = logging.getLogger("job_hunter.outreach.discovery")

CANDIDATES_CONFIG_PATH = PROJECT_ROOT / "config" / "candidate_companies.json"

# ATS-guess probing order. Unrelated to the careers-page path, which needs
# no ordering — a candidate either has a website worth probing or it
# doesn't.
PLATFORM_ORDER = ("greenhouse", "lever", "ashby", "smartrecruiters")

# Hard cap on slug guesses per company. Deliberately small: this is a
# handful of sensible spellings, not a permutation search. Every extra
# variant is a real request against a real company's API.
MAX_SLUG_VARIANTS = 3

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Words that mark a posting as graduate/junior/intern-shaped, for the
# priority tiering below. Deliberately plain substring checks against a
# short, unambiguous word list — this is a ranking hint, not a filter, and a
# false positive here only affects display order, never inclusion.
_GRADUATE_JUNIOR_WORDS = ("graduate", "junior", "intern", "internship", "trainee", "cadetship", "cadet")


@dataclass
class Candidate:
    """
    A company to investigate. `platform`/`identifier` are an optional ATS
    hint that saves probing — still verified before use. `website` is a
    real domain you (or Claude, via actual web research) confirmed — never
    guessed — and unlocks the careers-page probe for companies not on any
    of the four supported ATS platforms.
    """

    name: str
    platform: str = ""
    identifier: str = ""
    website: str = ""

    @property
    def has_hint(self) -> bool:
        return bool(self.platform and self.identifier)


@dataclass
class VerifiedBoard:
    """
    One company whose hiring this module found real, current evidence for —
    either an ATS API or a careers page it actually read.

    `jobs_unavailable` is the field that carries the distinction this module
    exists to make: True means "we found a real careers page but could not
    read structured job data from it" (a Workday/PageUp/etc. page, most
    commonly) — NOT "confirmed zero openings". It is False for every ATS
    result (that API call is authoritative, even when it returns nothing)
    and for a careers-page result that did find postings.
    """

    company: str
    platform: str
    identifier: str
    postings: list[NormalizedJob] = field(default_factory=list)
    website: str = ""
    jobs_unavailable: bool = False
    detected_system: str = ""
    careers_url: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def total_postings(self) -> int:
        return len(self.postings)

    @property
    def relevant_postings(self) -> list[NormalizedJob]:
        """
        Postings that clear the project's EXISTING entry-level filter
        (src/job_filter/cheap_filter.py) — the same deterministic rules the
        job pathway already uses to reject senior and non-IT roles. Reused
        rather than reimplemented so "relevant" means one thing everywhere.

        Location is deliberately not passed: the filter would otherwise
        reject a company for advertising this particular role interstate,
        when the question here is only "does this company hire the kind of
        work I could do".
        """
        return [
            posting
            for posting in self.postings
            if run_cheap_filter(posting.title, posting.description or "").passed
        ]

    @property
    def relevant_count(self) -> int:
        return len(self.relevant_postings)

    @property
    def priority_tier(self) -> int:
        """
        Where this company sits in your stated ranking:

          1. a current opening that clears the entry-level IT/cyber/software
             filter outright
          2. a current opening that reads as graduate/junior/intern AND
             names a recognised technology skill, without clearing tier 1
          3. any other current opening that names a recognised technology
             skill (a senior role still tells you this company hires tech)
          4. nothing above — either a confirmed-empty board, or a real,
             reachable careers page whose job data could not be read. Kept,
             never discarded; a later run may find something.

        Advisory ranking only. It orders what gets your attention first; it
        never removes a company from the registry (see run_discovery).
        """
        if self.relevant_count:
            return 1
        for posting in self.postings:
            text = f"{posting.title}\n{posting.description or ''}"
            is_grad_junior = any(word in text.lower() for word in _GRADUATE_JUNIOR_WORDS)
            if is_grad_junior and extract_skills(text):
                return 2
        if any(extract_skills(f"{p.title}\n{p.description or ''}") for p in self.postings):
            return 3
        return 4

    def _notes_text(self) -> str:
        """
        A one-line, human-checkable account of what was found and how. When
        the careers resolver walked links to get here, the trail it recorded
        is appended verbatim — so every registry entry can be traced back to
        the page that produced it rather than being taken on trust.
        """
        today = date.today().isoformat()
        if self.platform == "careers_page":
            if self.jobs_unavailable:
                system = f" ({self.detected_system})" if self.detected_system else ""
                note = (
                    f"Discovered {today}: careers page verified at {self.identifier}{system}, "
                    f"but job data is not machine-readable — check it by hand."
                )
            else:
                note = (
                    f"Discovered {today} via its own careers page ({self.identifier}): "
                    f"{self.total_postings} open posting(s), {self.relevant_count} entry-level/IT relevant."
                )
        else:
            note = (
                f"Discovered {today} via {self.platform}: "
                f"{self.total_postings} open posting(s), {self.relevant_count} entry-level/IT relevant"
            )
        if self.evidence:
            note = f"{note} [via {' -> '.join(self.evidence[-2:])}]"
        return note

    def to_config_entry(self) -> dict:
        """
        The shape targets.py expects. contact_email is left blank on
        purpose — this module verifies job boards, not addresses; the
        outreach pipeline's own contact-discovery step (or you, by hand)
        fills that in.
        """
        return {
            "company": self.company,
            "website": self.website,
            "platform": self.platform,
            "identifier": self.identifier,
            "contact_email": "",
            "notes": self._notes_text(),
            "enabled": True,
        }


@dataclass
class DiscoveryResult:
    researched: int = 0
    verified: list[VerifiedBoard] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)

    @property
    def with_relevant_openings(self) -> list[VerifiedBoard]:
        return [board for board in self.verified if board.relevant_count > 0]

    @property
    def by_tier(self) -> list[VerifiedBoard]:
        """All verified boards, best opportunity first — reporting order
        only, never an exclusion."""
        return sorted(self.verified, key=lambda b: (b.priority_tier, -b.total_postings))

    def to_dict(self) -> dict:
        return {
            "researched": self.researched,
            "verified": len(self.verified),
            "with_relevant_openings": len(self.with_relevant_openings),
            "by_tier": {tier: len([b for b in self.verified if b.priority_tier == tier]) for tier in (1, 2, 3, 4)},
            "added": self.added,
            "already_present": self.already_present,
            "unverified": self.unverified,
        }


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------

def load_candidates(config_path: Path | None = None) -> list[Candidate]:
    """
    Read config/candidate_companies.json. Never raises on a malformed entry —
    logs and skips it, same convention as the other registries. Accepts a
    bare string, or an object with an optional platform/identifier hint
    and/or an optional `website` — a real domain, never guessed here or
    anywhere downstream of this function.
    """
    path = config_path or CANDIDATES_CONFIG_PATH
    if not path.exists():
        logger.warning("Candidate list not found at %s — nothing to discover.", path)
        return []

    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read candidate list at %s: %s", path, exc)
        return []

    if not isinstance(raw, list):
        logger.error("Candidate list at %s must be a JSON array.", path)
        return []

    candidates: list[Candidate] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                candidates.append(Candidate(name=name))
            continue
        if not isinstance(entry, dict):
            logger.warning("Skipping malformed candidate entry %r", entry)
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            logger.warning("Skipping candidate entry with no name: %r", entry)
            continue
        platform = str(entry.get("platform") or "").strip().lower()
        identifier = str(entry.get("identifier") or "").strip()
        if platform and platform not in ADAPTERS:
            logger.warning("Candidate %r hints unsupported platform %r — ignoring the hint.", name, platform)
            platform, identifier = "", ""
        website = str(entry.get("website") or "").strip()
        candidates.append(Candidate(name=name, platform=platform, identifier=identifier, website=website))
    return candidates


def slug_variants(name: str) -> list[str]:
    """
    A few sensible ATS slugs for a company name, most-likely first:
    "Culture Amp" -> ["cultureamp", "culture-amp", "cultureamp"] deduped.

    Capped at MAX_SLUG_VARIANTS. This is intentionally not a permutation
    search — each variant costs a real request against a real company's API.
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return []
    words = [w for w in _NON_ALNUM.split(lowered) if w]
    if not words:
        return []

    variants = [
        "".join(words),        # cultureamp
        "-".join(words),       # culture-amp
        words[0],              # culture  (common for one-word brand slugs)
    ]
    seen: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.append(variant)
    return seen[:MAX_SLUG_VARIANTS]


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def verify_board(
    company: str,
    platform: str,
    identifier: str,
    *,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
) -> VerifiedBoard | None:
    """
    Ask one ATS platform's real API whether this identifier is a live board
    with postings. Returns None for anything else — a 404, an error, an
    empty board, or an unsupported platform.

    An empty board counts as unverified on purpose here: for a GUESSED
    slug, a board that resolves but lists nothing is no evidence the guess
    even points at the right company. (Contrast with a careers-page probe,
    where the domain itself is already known real — see verify_careers_page.)
    """
    adapter = (adapters if adapters is not None else ADAPTERS).get(platform)
    if adapter is None:
        return None
    try:
        postings = adapter.discover(identifier)
    except Exception as exc:  # noqa: BLE001 — a wrong guess is the normal case, not an error
        logger.debug("No %s board at %r for %r: %s", platform, identifier, company, exc)
        return None
    if not postings:
        return None
    return VerifiedBoard(company=company, platform=platform, identifier=identifier, postings=postings)


def verify_careers_page(
    company: str,
    website: str,
    *,
    fetch=polite_get,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
) -> VerifiedBoard | None:
    """
    Probe a company's OWN, already-verified domain for its careers system
    (see probe_careers_page, which crawls it via careers_resolver).

    Three outcomes, best first:

      1. The crawl found a recognised ATS whose adapter this project has,
         with a tenant/site/slug read out of the company's own HTML. That
         adapter is queried and, if it answers, the company is registered
         under the REAL platform — no longer "careers_page" — with real
         postings. This is the upgrade the whole resolver exists for.
      2. Structured JobPosting data on the careers page itself.
      3. Reachable but unreadable. Still a verified result, exactly as
         before: the domain is real and the page is real, this module simply
         cannot machine-read what it lists. Never "confirmed zero".

    Unlike verify_board, an ATS that answers with an EMPTY list is trusted
    here: the identifier came from the company's own site rather than from a
    name-derived guess, so "this employer currently advertises nothing" is a
    real answer rather than evidence the guess was wrong.

    Returns None only when no careers/jobs page could be reached at all.
    """
    result = probe_careers_page(website, fetch=fetch)
    if not result.reachable:
        return None

    board = VerifiedBoard(
        company=company,
        platform="careers_page",
        identifier=result.url,
        postings=result.postings,
        website=website,
        jobs_unavailable=not result.jobs_available,
        detected_system=result.detected_system,
        careers_url=result.url,
        evidence=list(result.evidence),
    )

    if result.platform and result.ats_identifier:
        adapter = (adapters if adapters is not None else ADAPTERS).get(result.platform)
        if adapter is not None:
            try:
                postings = adapter.discover(result.ats_identifier)
            except Exception as exc:  # noqa: BLE001 — a dead board falls back, it does not crash
                logger.info(
                    "%s board %s (found on %r's own site) did not answer: %s",
                    result.platform, result.ats_identifier, company, exc,
                )
            else:
                board.platform = result.platform
                board.identifier = result.ats_identifier
                board.postings = postings
                board.jobs_unavailable = False
    return board


def discover_company(
    candidate: Candidate,
    *,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
    fetch=polite_get,
) -> VerifiedBoard | None:
    """
    Find real evidence of one company's current hiring, or return None.

    Order: a verified ATS hint first (cheapest, most authoritative when it's
    right); otherwise exactly one of the two remaining paths, chosen by what
    the candidate actually carries — never both:

      - A known real website -> straight to the careers-page probe. Slug-
        guessing a company you already have a verified domain for is pure
        waste against shared ATS hosts: none of the ~400 non-ATS-hinted
        employers in this registry (banks, universities, government
        agencies, engineering firms...) are meaningfully likely to be on
        Greenhouse, Lever, Ashby or SmartRecruiters, so guessing for every
        one of them would multiply requests against those four hosts for
        almost no yield. A company that both HAS a website and genuinely
        IS on one of the four platforms is still findable the normal way —
        by giving it a platform/identifier hint in candidate_companies.json,
        the same as any other confirmed ATS board.
      - No website at all -> ATS slug-guessing is the only avenue left, so
        it still runs for that small remainder.

    A company with no ATS hint, no website, and no guessable ATS board
    simply cannot be verified by this module; that is the correct outcome,
    not an error.
    """
    if candidate.has_hint:
        # Verify the hint rather than trusting it. A stale hint falls through
        # to normal probing.
        board = verify_board(
            candidate.name, candidate.platform, candidate.identifier, adapters=adapters
        )
        if board is not None:
            return board
        logger.info("Hint for %r (%s/%s) no longer resolves — probing instead.",
                    candidate.name, candidate.platform, candidate.identifier)

    if candidate.website:
        return verify_careers_page(candidate.name, candidate.website, fetch=fetch, adapters=adapters)

    for platform in PLATFORM_ORDER:
        for identifier in slug_variants(candidate.name):
            board = verify_board(candidate.name, platform, identifier, adapters=adapters)
            if board is not None:
                return board

    return None


def deduplicate(boards: list[VerifiedBoard]) -> list[VerifiedBoard]:
    """
    One entry per real company. Two keys are needed, not one:

      - the normalized company NAME, so a company verified twice collapses
        to the board with the most postings (the fuller picture of what
        they hire for), with an ATS API result preferred over a
        careers-page result at an equal count (the more authoritative
        source when both exist);
      - the (platform, identifier) BOARD, because different candidate names
        can resolve to the same board — "Deputy" and "Deputy AU" both land
        on lever/deputy. Keying on name alone would emit that company twice
        and the outreach pipeline would then treat them as two targets.
    """
    def _rank(board: VerifiedBoard) -> tuple[int, int]:
        return (board.total_postings, 0 if board.platform != "careers_page" else -1)

    best_by_name: dict[str, VerifiedBoard] = {}
    for board in boards:
        key = normalize_for_dedupe(board.company)
        current = best_by_name.get(key)
        if current is None or _rank(board) > _rank(current):
            best_by_name[key] = board

    # Second pass: collapse distinct names that point at one board, keeping
    # the shortest name (the plainest form — "Deputy" over "Deputy AU").
    best_by_board: dict[tuple[str, str], VerifiedBoard] = {}
    for board in best_by_name.values():
        board_key = (board.platform, board.identifier)
        current = best_by_board.get(board_key)
        if current is None or len(board.company) < len(current.company):
            best_by_board[board_key] = board
    return list(best_by_board.values())


# --------------------------------------------------------------------------
# Merging into the outreach registry
# --------------------------------------------------------------------------

def _read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s: %s — refusing to overwrite it.", path, exc)
        raise
    return raw if isinstance(raw, list) else []


def merge_into_outreach_config(
    boards: list[VerifiedBoard],
    *,
    config_path: Path | None = None,
    write: bool = False,
) -> tuple[list[str], list[str]]:
    """
    Add newly-verified companies to config/outreach_companies.json.

    Every verified board is added, regardless of priority tier — a company
    hiring nothing relevant to you today is still a legitimate technology
    employer worth having in the registry; the existing outreach pipeline
    re-researches it on every run and will notice on its own if that
    changes. Filtering by tier is the caller's choice for what to WRITE
    (see run_discovery's `min_tier`), not something this function does.

    Existing entries are never modified — not their contact_email, not their
    notes, not their enabled flag. Anything you typed by hand survives a
    discovery run untouched; this only ever appends companies that aren't
    already listed. Returns (added_names, already_present_names).
    """
    path = config_path or OUTREACH_CONFIG_PATH
    existing = _read_existing(path)
    known = {
        normalize_for_dedupe(str(entry.get("company", "")))
        for entry in existing
        if isinstance(entry, dict)
    }

    added: list[str] = []
    already: list[str] = []
    for board in boards:
        if normalize_for_dedupe(board.company) in known:
            already.append(board.company)
            continue
        existing.append(board.to_config_entry())
        known.add(normalize_for_dedupe(board.company))
        added.append(board.company)

    if write and added:
        path.write_text(json.dumps(existing, indent=2) + "\n")
        logger.info("Added %d verified companies to %s", len(added), path)
    return added, already


# --------------------------------------------------------------------------
# Refreshing companies already on file
# --------------------------------------------------------------------------

@dataclass
class RefreshResult:
    """What one pass over the already-registered careers_page entries did."""

    processed: int = 0
    upgraded: list[str] = field(default_factory=list)       # now on a queryable ATS
    reresolved: list[str] = field(default_factory=list)     # better careers URL, same platform
    unchanged: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    boards: list[VerifiedBoard] = field(default_factory=list)
    detected_only: dict[str, int] = field(default_factory=dict)


def refresh_careers_page_entries(
    *,
    config_path: Path | None = None,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
    fetch=polite_get,
    write: bool = False,
    limit: int | None = None,
) -> RefreshResult:
    """
    Re-resolve every company already registered as `careers_page` and
    upgrade the ones whose real ATS the resolver can now find.

    This is the one place in this module that MODIFIES an existing registry
    entry rather than appending a new one, and it is deliberately narrow:

      * Only entries whose platform is currently "careers_page" are touched.
        A company already on Greenhouse/Lever/Ashby/SmartRecruiters/Workday
        is left completely alone.
      * Only `platform`, `identifier` and `notes` are ever rewritten.
        `contact_email` and `enabled` are carried across untouched — an
        address you found by hand, or a company you deliberately switched
        off, must survive any number of discovery runs. Any other key an
        entry carries is preserved as-is too.
      * An entry whose careers page has stopped responding is left exactly
        as it was and reported as unreachable, never downgraded or removed:
        a site being down today is not evidence the company isn't real.

    Nothing is written unless `write=True`.
    """
    path = config_path or OUTREACH_CONFIG_PATH
    entries = _read_existing(path)
    result = RefreshResult()

    targets = [
        entry for entry in entries
        if isinstance(entry, dict)
        and str(entry.get("platform", "")).strip() == "careers_page"
        and str(entry.get("website", "")).strip()
    ]
    if limit is not None:
        targets = targets[:limit]

    changed = False
    for entry in targets:
        result.processed += 1
        company = str(entry.get("company", ""))
        website = str(entry.get("website", "")).strip()

        logger.info("[%d/%d] re-resolving %s (%s)", result.processed, len(targets), company, website)
        board = verify_careers_page(company, website, fetch=fetch, adapters=adapters)
        if board is None:
            result.unreachable.append(company)
            logger.info("    unreachable this run — entry left untouched")
            continue

        result.boards.append(board)
        if board.detected_system and board.platform == "careers_page":
            result.detected_only[board.detected_system] = result.detected_only.get(board.detected_system, 0) + 1

        previous_identifier = str(entry.get("identifier", ""))
        if board.platform != "careers_page":
            result.upgraded.append(f"{company}: careers_page -> {board.platform}")
            changed = True
        elif board.identifier and board.identifier != previous_identifier:
            result.reresolved.append(f"{company}: {previous_identifier} -> {board.identifier}")
            changed = True
        else:
            result.unchanged.append(company)
            continue

        logger.info(
            "    -> %s / %s (%d posting(s), %d relevant)",
            board.platform, board.identifier, board.total_postings, board.relevant_count,
        )

        # contact_email and enabled are pointedly NOT in this list.
        entry["platform"] = board.platform
        entry["identifier"] = board.identifier
        entry["notes"] = board._notes_text()

    if write and changed:
        path.write_text(json.dumps(entries, indent=2) + "\n")
        logger.info(
            "Refreshed %s: %d upgraded to a queryable ATS, %d careers URLs re-resolved.",
            path, len(result.upgraded), len(result.reresolved),
        )
    return result


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_discovery(
    *,
    candidates_path: Path | None = None,
    config_path: Path | None = None,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
    fetch=polite_get,
    write: bool = False,
    limit: int | None = None,
    min_tier: int = 4,
) -> DiscoveryResult:
    """
    Probe every candidate, keep the verified ones, and (when write=True) add
    them to the outreach registry.

    min_tier=4 (the default) keeps every verified company, including tier 4
    — "legitimate employer, nothing current matches" — per the standing
    rule that a company is never discarded just because it isn't hiring for
    something relevant today. Pass a lower number (e.g. 1) only if you
    deliberately want a narrower run.

    A candidate already present in the outreach registry (by name) is
    skipped before any network call — re-verifying a company already on
    file wastes a round of requests for a result that would be thrown away
    at merge time anyway. This is what makes a second run over the same
    candidate list cheap: only genuinely new candidates cost anything.
    """
    candidates = load_candidates(candidates_path)
    if limit is not None:
        candidates = candidates[:limit]

    already_known = {
        normalize_for_dedupe(str(entry.get("company", "")))
        for entry in _read_existing(config_path or OUTREACH_CONFIG_PATH)
        if isinstance(entry, dict)
    }

    result = DiscoveryResult(researched=len(candidates))
    boards: list[VerifiedBoard] = []

    for candidate in candidates:
        if normalize_for_dedupe(candidate.name) in already_known:
            result.already_present.append(candidate.name)
            continue

        board = discover_company(candidate, adapters=adapters, fetch=fetch)
        if board is None:
            result.unverified.append(candidate.name)
            continue
        boards.append(board)
        if board.jobs_unavailable:
            logger.info(
                "Verified %r via its careers page (%s) — job data not machine-readable.",
                board.company, board.identifier,
            )
        else:
            logger.info(
                "Verified %r on %s/%s — %d posting(s), %d relevant, tier %d.",
                board.company, board.platform, board.identifier,
                board.total_postings, board.relevant_count, board.priority_tier,
            )

    result.verified = deduplicate(boards)
    selected = [b for b in result.verified if b.priority_tier <= min_tier]
    added, already_from_merge = merge_into_outreach_config(
        selected, config_path=config_path, write=write
    )
    # already_present accumulates both skip points: candidates recognised
    # before any network call, and the (now rare) case merge_into_outreach_config
    # still catches — a board discovered under a different spelling of a
    # name already on file.
    result.added = added
    result.already_present.extend(already_from_merge)
    return result


def _summarise(boards: list[VerifiedBoard]) -> dict:
    """Counts for the report, derived only from what the boards actually
    contain — no estimates, no extrapolation from a sample."""
    by_platform: dict[str, int] = {}
    for board in boards:
        by_platform[board.platform] = by_platform.get(board.platform, 0) + 1
    return {
        "by_platform": dict(sorted(by_platform.items(), key=lambda kv: -kv[1])),
        "postings": sum(b.total_postings for b in boards),
        "relevant": sum(b.relevant_count for b in boards),
        "with_postings": len([b for b in boards if b.total_postings]),
        "with_relevant": len([b for b in boards if b.relevant_count]),
        "unreadable": len([b for b in boards if b.jobs_unavailable]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify candidate companies — against their real public ATS board where one of the "
            "supported platforms applies, or against their own careers page otherwise — and "
            "add the confirmed ones to the outreach registry. Nothing is written without --write."
        )
    )
    parser.add_argument("--write", action="store_true", help="Actually update config/outreach_companies.json.")
    parser.add_argument("--limit", type=int, default=None, help="Only check the first N candidates.")
    parser.add_argument(
        "--refresh", action="store_true",
        help="Also re-resolve companies already registered as careers_page, upgrading any whose "
             "real ATS the resolver can now find. Never touches contact_email or enabled.",
    )
    parser.add_argument(
        "--refresh-only", action="store_true",
        help="Run only the refresh pass over existing careers_page entries; skip new candidates.",
    )
    parser.add_argument(
        "--relevant-only", action="store_true",
        help="Only keep tier-1 companies (a current entry-level opening). Off by default — a "
             "company is not discarded just because nothing matches today.",
    )
    parser.add_argument("--candidates", default=None, help="Candidate list path.")
    parser.add_argument("--config", default=None, help="Outreach registry path.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config_path = Path(args.config) if args.config else None

    refresh: RefreshResult | None = None
    if args.refresh or args.refresh_only:
        refresh = refresh_careers_page_entries(
            config_path=config_path, write=args.write, limit=args.limit
        )

    result = DiscoveryResult()
    if not args.refresh_only:
        result = run_discovery(
            candidates_path=Path(args.candidates) if args.candidates else None,
            config_path=config_path,
            write=args.write,
            limit=args.limit,
            min_tier=1 if args.relevant_only else 4,
        )

    all_boards = list(result.verified) + (refresh.boards if refresh else [])
    stats = _summarise(all_boards)
    suffix = "" if args.write else "   (dry run — pass --write to save)"

    print("\n================ DISCOVERY REPORT ================")
    print(f"Candidates researched:              {result.researched}")
    if refresh:
        print(f"Existing careers_page re-resolved:  {refresh.processed}")
    print(f"Total companies processed:          {result.researched + (refresh.processed if refresh else 0)}")
    print(f"Careers systems resolved:           {len(all_boards)}")
    print("\n-- platforms actually discovered --")
    for platform, count in stats["by_platform"].items():
        print(f"  {platform:<18} {count}")
    if refresh and refresh.detected_only:
        print("\n-- ATS recognised but NOT queryable (no verified public endpoint yet) --")
        for label, count in sorted(refresh.detected_only.items(), key=lambda kv: -kv[1]):
            print(f"  {label:<18} {count}")
    print("\n-- job data --")
    print(f"  companies with real postings:     {stats['with_postings']}")
    print(f"  total postings read:              {stats['postings']}")
    print(f"  companies with relevant openings: {stats['with_relevant']}")
    print(f"  relevant IT/cyber/software roles: {stats['relevant']}")
    print(f"  careers page real but unreadable: {stats['unreadable']}")
    print(f"\nCompletely unverified candidates:   {len(result.unverified)}")
    print(f"Added to registry:                  {len(result.added)}{suffix}")
    if refresh:
        print(f"Upgraded in registry:               {len(refresh.upgraded)}{suffix}")
        print(f"Careers URL re-resolved:            {len(refresh.reresolved)}")
        print(f"Unreachable on this run (kept):     {len(refresh.unreachable)}")
    print("=================================================\n")

    if refresh and refresh.upgraded:
        print("Upgrades:")
        for line in refresh.upgraded:
            print(f"  {line}")
    for board in sorted(all_boards, key=lambda b: (b.priority_tier, -b.total_postings))[:60]:
        status = "unavailable" if board.jobs_unavailable else f"{board.relevant_count}/{board.total_postings} relevant"
        print(f"  tier {board.priority_tier}  {board.company:<28} {board.platform:<16} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
