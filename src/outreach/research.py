"""
Outreach research — read a company's REAL, currently-published job postings.

Sources are limited to the four public ATS APIs the job pathway already
uses, through the existing adapters in src/job_discovery/sources/ (Greenhouse,
Lever, Ashby, SmartRecruiters). Those adapters are reused as-is via
job_discovery.registry.ADAPTERS — this module contains no HTTP code of its
own, so the per-host rate limiting and identifying User-Agent in
job_discovery.base.polite_get apply here for free, and there is exactly one
API client per platform in the codebase.

Nothing here scrapes LinkedIn, SEEK, Indeed, Google, or any rendered web
page, and nothing here calls a paid service.

The extraction below is deliberately simple: it slices up text that is
literally present in a real posting (requirement lines, experience phrases,
recognised technology names) so a later step can hand Ollama something
structured. It is an index of the source text, not an analysis of it — there
is no scoring, no ranking, and no judgement about whether a company is worth
contacting. That decision belongs to a later layer.

This module never decides who gets emailed and never sends anything.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from src.database.models import normalize_for_dedupe
from src.job_discovery.base import JobDiscoverySource
from src.job_discovery.registry import ADAPTERS, load_employers
from src.sources.base import NormalizedJob

from .relevance import LocationRelevance, au_relevance

logger = logging.getLogger("job_hunter.outreach.research")

# Where an ATS board came from, recorded so a later step (and the user) can
# see why a given board was read for a given company.
RESOLVED_FROM_COMPANY = "company_record"
RESOLVED_FROM_EMPLOYERS = "employers_config"

# Headings that introduce a requirements/qualifications block in a posting.
_REQUIREMENT_HEADINGS = (
    "requirement", "qualification", "what you'll need", "what you will need",
    "what we're looking for", "what we are looking for", "about you", "you have",
    "you'll bring", "you will bring", "skills and experience", "essential",
    "who you are", "must have", "minimum qualifications",
)

# A line that looks like a section heading rather than content: short, and
# either ends with a colon or has no sentence punctuation at all.
_HEADING_MAX_WORDS = 10

_BULLET_PREFIXES = ("-", "*", "•", "·", "–", "—", "‣", "◦")

# Sentences mentioning years of experience. Captures the surrounding phrase
# rather than just the number, because "2+ years in a helpdesk role" is far
# more useful downstream than the bare integer 2.
_EXPERIENCE_RE = re.compile(
    r"[^.\n]*?\b\d+\s*(?:\+|-\s*\d+)?\s*(?:or more\s*)?years?\b[^.\n]*",
    re.IGNORECASE,
)

# Recognised technology/skill names, matched case-insensitively as whole
# words against the posting text. A flat lookup list on purpose — this is a
# convenience index over words the posting actually contains, so a later
# prompt can point at them. It is not weighted, counted, or scored.
_TECH_TERMS: dict[str, tuple[str, ...]] = {
    "Python": (r"python",),
    "JavaScript": (r"javascript", r"node\.?js"),
    "TypeScript": (r"typescript",),
    "Java": (r"java(?!script)",),
    "C#": (r"c#", r"\.net"),
    "Go": (r"golang",),
    "PHP": (r"php",),
    "Ruby": (r"ruby",),
    "PowerShell": (r"powershell",),
    "Bash": (r"bash", r"shell scripting"),
    "SQL": (r"sql", r"postgres(?:ql)?", r"mysql"),
    "React": (r"react",),
    "Linux": (r"linux", r"ubuntu"),
    "Windows Server": (r"windows server", r"active directory"),
    "AWS": (r"\baws\b", r"amazon web services"),
    "Azure": (r"azure", r"microsoft 365", r"office 365", r"intune"),
    "Google Cloud": (r"\bgcp\b", r"google cloud"),
    "Networking": (r"networking", r"tcp/ip", r"\bdns\b", r"\bdhcp\b", r"firewall", r"cisco"),
    "Cybersecurity": (r"cyber ?security", r"information security", r"infosec"),
    "Penetration Testing": (r"penetration testing", r"\bkali\b", r"vulnerability assessment"),
    "SIEM": (r"\bsiem\b", r"splunk"),
    "Docker": (r"docker",),
    "Kubernetes": (r"kubernetes", r"\bk8s\b"),
    "CI/CD": (r"ci/cd", r"continuous integration", r"jenkins", r"github actions"),
    "Git": (r"\bgit\b", r"github", r"gitlab"),
    "Terraform": (r"terraform", r"infrastructure as code"),
    "Automation": (r"automation", r"automate", r"scripting"),
    "Troubleshooting": (r"troubleshoot", r"fault finding"),
    "Technical Support": (r"technical support", r"service desk", r"help ?desk"),
    "Customer Support": (r"customer support", r"customer service"),
    "Ticketing Systems": (r"ticketing", r"\bjira\b", r"servicenow", r"zendesk"),
    "ITIL": (r"\bitil\b",),
    "QA Testing": (r"quality assurance", r"\bqa\b", r"manual testing", r"test cases"),
    "Test Automation": (r"test automation", r"selenium", r"playwright", r"cypress"),
    "Data Analysis": (r"data analysis", r"power ?bi", r"tableau"),
    "APIs": (r"\bapis?\b", r"restful"),
    "Agile": (r"\bagile\b", r"scrum", r"kanban"),
}

_COMPILED_TECH_TERMS = {
    label: re.compile("|".join(patterns), re.IGNORECASE) for label, patterns in _TECH_TERMS.items()
}

# Caps, so one verbose posting can't dominate a stored research blob.
MAX_REQUIREMENTS_PER_POSTING = 12
MAX_EXPERIENCE_PHRASES = 5
MAX_DESCRIPTION_CHARS = 8000


@dataclass
class AtsTarget:
    """Which public board a company's postings should be read from."""

    platform: str
    identifier: str
    resolved_from: str

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "identifier": self.identifier,
            "resolved_from": self.resolved_from,
        }


@dataclass
class PostingResearch:
    """
    One real posting, plus plain slices of its own text. Every field here
    traces back to what the ATS API returned — nothing is inferred, and
    nothing is added that the posting did not say.
    """

    title: str
    url: str
    location: str
    description: str
    source: str = ""
    source_job_id: str = ""
    requirements: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)

    @classmethod
    def from_normalized(cls, job: NormalizedJob) -> "PostingResearch":
        description = (job.description or "").strip()[:MAX_DESCRIPTION_CHARS]
        return cls(
            title=(job.title or "").strip(),
            url=job.url or "",
            location=(job.location or "").strip(),
            description=description,
            source=job.source or "",
            source_job_id=job.source_job_id or "",
            requirements=extract_requirements(description),
            skills=extract_skills(f"{job.title}\n{description}"),
            experience=extract_experience(description),
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "location": self.location,
            "description": self.description,
            "source": self.source,
            "source_job_id": self.source_job_id,
            "requirements": self.requirements,
            "skills": self.skills,
            "experience": self.experience,
        }


@dataclass
class CompanyResearch:
    """
    Everything legitimately gathered about one company's current hiring.

    `error` being set and `postings` being empty are both normal outcomes,
    not failures to handle specially: a company with no readable board
    simply has no posting evidence, and a later step is expected to treat
    that as "nothing specific to say" rather than filling the gap.

    `location_relevance` records how many of the postings match the
    app's target locations (Sydney/NSW/Remote Australia by default).
    Used by the pipeline and dashboard to keep outreach honest about
    AU relevance: 609 open postings, 0 of them in Australia is very
    different from 609 open postings, 40 in Sydney, and the record
    has to say which.
    """

    company: str
    platform: str = ""
    identifier: str = ""
    resolved_from: str = ""
    postings: list[PostingResearch] = field(default_factory=list)
    fetched_at: str = ""
    error: str = ""
    location_relevance: LocationRelevance = field(default_factory=lambda: LocationRelevance(0, 0))

    @property
    def has_postings(self) -> bool:
        return bool(self.postings)

    @property
    def skills(self) -> list[str]:
        """
        Every technology named across the postings, in first-seen order.
        A convenience union, not a frequency ranking.
        """
        seen: list[str] = []
        for posting in self.postings:
            for skill in posting.skills:
                if skill not in seen:
                    seen.append(skill)
        return seen

    @property
    def titles(self) -> list[str]:
        return list(dict.fromkeys(p.title for p in self.postings if p.title))

    def to_dict(self) -> dict:
        return {
            "company": self.company,
            "platform": self.platform,
            "identifier": self.identifier,
            "resolved_from": self.resolved_from,
            "fetched_at": self.fetched_at,
            "postings": [p.to_dict() for p in self.postings],
            "titles": self.titles,
            "skills": self.skills,
            "location_relevance": self.location_relevance.to_dict(),
            "error": self.error or None,
        }

    def evidence_snapshot(self, max_postings: int = 15, max_chars: int = 2000) -> dict:
        """
        The posting text an email was written from, trimmed to roughly what
        the model actually saw (personalization.py shows it 1800 chars per
        posting). Stored alongside the message so the quality gate can
        re-verify wording against the real evidence later without refetching
        anyone's job board, while keeping the stored blob a sane size.
        """
        return {
            "company": self.company,
            "platform": self.platform,
            "identifier": self.identifier,
            "fetched_at": self.fetched_at,
            "postings": [
                {
                    "title": p.title,
                    "url": p.url,
                    "location": p.location,
                    "description": (p.description or "")[:max_chars],
                }
                for p in self.postings[:max_postings]
            ],
        }

    def summary(self) -> dict:
        """
        A compact version for storage and display: everything the review UI
        needs, without the full posting descriptions. Those can run to
        several thousand characters each, and a company with forty postings
        would otherwise put a very large blob in every database row for
        information the dashboard never shows.
        """
        return {
            "company": self.company,
            "platform": self.platform,
            "identifier": self.identifier,
            "resolved_from": self.resolved_from,
            "fetched_at": self.fetched_at,
            "posting_count": len(self.postings),
            "titles": self.titles,
            "skills": self.skills,
            "location_relevance": self.location_relevance.to_dict(),
            "postings": [
                {"title": p.title, "url": p.url, "location": p.location}
                for p in self.postings
            ],
            "error": self.error or None,
        }


# --------------------------------------------------------------------------
# ATS resolution
# --------------------------------------------------------------------------

def resolve_ats(
    name: str,
    platform: str = "",
    identifier: str = "",
    *,
    employers: list | None = None,
) -> AtsTarget | None:
    """
    Work out which public board to read for a company.

    The company's own recorded platform/identifier wins. Failing that, the
    company name is matched against config/employers.json — the registry of
    boards already verified for the job pathway — so a company configured
    there needs no second configuration here.

    Returns None when no supported board is known, which is a normal answer,
    not an error.
    """
    platform = (platform or "").strip().lower()
    identifier = (identifier or "").strip()
    if platform and identifier:
        if platform not in ADAPTERS:
            logger.warning(
                "Company %r is configured with unsupported platform %r — ignoring it. Supported: %s",
                name, platform, sorted(ADAPTERS),
            )
        else:
            return AtsTarget(platform, identifier, RESOLVED_FROM_COMPANY)

    target_name = normalize_for_dedupe(name)
    if not target_name:
        return None

    for employer in employers if employers is not None else load_employers():
        if normalize_for_dedupe(employer.company) != target_name:
            continue
        if employer.platform not in ADAPTERS:
            continue
        return AtsTarget(employer.platform, employer.identifier, RESOLVED_FROM_EMPLOYERS)
    return None


# --------------------------------------------------------------------------
# Research
# --------------------------------------------------------------------------

def research_company(
    name: str,
    platform: str = "",
    identifier: str = "",
    *,
    employers: list | None = None,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
) -> CompanyResearch:
    """
    Fetch one company's currently-published postings from its public ATS
    board and extract the useful parts of each.

    Never raises. A missing board, a renamed board, or a network failure is
    recorded on `error` and leaves `postings` empty, so one bad company
    can't stop a batch.
    """
    research = CompanyResearch(
        company=name,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    target = resolve_ats(name, platform, identifier, employers=employers)
    if target is None:
        research.error = "no supported ATS board configured for this company"
        logger.info("No ATS board known for %r — no postings gathered.", name)
        return research

    research.platform = target.platform
    research.identifier = target.identifier
    research.resolved_from = target.resolved_from

    adapter = (adapters if adapters is not None else ADAPTERS).get(target.platform)
    if adapter is None:
        research.error = f"unsupported platform {target.platform!r}"
        return research

    try:
        postings = adapter.discover(target.identifier)
    except Exception as exc:  # noqa: BLE001 — one dead board must not stop a batch
        research.error = f"job board fetch failed: {exc}"
        logger.warning(
            "Could not read %s board %r for %r: %s", target.platform, target.identifier, name, exc
        )
        return research

    research.postings = [PostingResearch.from_normalized(job) for job in postings]
    # AU relevance is computed from the same location text the postings
    # came back with — reusing src.job_filter.location.location_matches
    # via src.outreach.relevance.au_relevance, so this stage and the
    # job-hunt UI can never disagree on what "in Sydney" means.
    research.location_relevance = au_relevance(p.location for p in research.postings)
    logger.info(
        "Found %d public posting(s) for %r via %s board %r (%d in target region).",
        len(research.postings), name, target.platform, target.identifier,
        research.location_relevance.matching,
    )
    return research


def research_company_record(
    company: Mapping[str, Any],
    *,
    employers: list | None = None,
    adapters: Mapping[str, JobDiscoverySource] | None = None,
) -> CompanyResearch:
    """
    Convenience wrapper for an outreach_companies row (a sqlite3.Row works,
    being a Mapping) so callers don't have to unpack the same three fields.
    """
    return research_company(
        company["name"],
        _optional(company, "platform"),
        _optional(company, "identifier"),
        employers=employers,
        adapters=adapters,
    )


def _optional(company: Mapping[str, Any], key: str) -> str:
    """
    Read a column that may be absent (an older row) or NULL, without the
    KeyError a sqlite3.Row raises for an unknown column.
    """
    try:
        return company[key] or ""
    except (KeyError, IndexError):
        return ""


# --------------------------------------------------------------------------
# Text extraction — slices of the posting's own words, nothing invented
# --------------------------------------------------------------------------

def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped.split()) > _HEADING_MAX_WORDS:
        return False
    # A bulleted line is content, never a heading — without this, a short
    # requirement like "- Basic scripting in PowerShell" looks exactly like
    # a heading (few words, no terminal full stop) and gets skipped.
    if stripped.startswith(_BULLET_PREFIXES):
        return False
    return stripped.endswith(":") or not stripped.endswith((".", "!", "?"))


def _strip_bullet(line: str) -> str:
    stripped = line.strip()
    for prefix in _BULLET_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def extract_requirements(description: str) -> list[str]:
    """
    Pull the requirement-ish lines out of a posting.

    Two passes: lines following a "Requirements"/"What you'll need"-style
    heading, and any bulleted line anywhere in the posting. Both return the
    posting's own sentences verbatim — this reformats text, it does not
    summarise or rewrite it.
    """
    if not description:
        return []

    lines = description.splitlines()
    found: list[str] = []
    in_requirements = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        lowered = stripped.lower()
        if _is_heading(stripped):
            in_requirements = any(heading in lowered for heading in _REQUIREMENT_HEADINGS)
            continue

        is_bullet = stripped.startswith(_BULLET_PREFIXES)
        if not (in_requirements or is_bullet):
            continue

        text = _strip_bullet(stripped)
        # Skip fragments too short to mean anything and boilerplate so long
        # it's clearly a paragraph rather than a requirement.
        if not (10 <= len(text) <= 300):
            continue
        if text not in found:
            found.append(text)
        if len(found) >= MAX_REQUIREMENTS_PER_POSTING:
            break

    return found


def extract_experience(description: str) -> list[str]:
    """
    Phrases in the posting that state an experience requirement, e.g.
    "2+ years of experience in a service desk role". Returned as written.
    """
    if not description:
        return []
    phrases: list[str] = []
    for match in _EXPERIENCE_RE.finditer(description):
        phrase = " ".join(match.group(0).split()).strip(" -•·–—")
        if phrase and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= MAX_EXPERIENCE_PHRASES:
            break
    return phrases


def extract_skills(text: str) -> list[str]:
    """
    Technology/skill names the posting actually mentions, in the canonical
    spelling from _TECH_TERMS. A term appears only if its pattern matches
    the posting text, so this can never introduce a technology the company
    did not name.
    """
    if not text:
        return []
    return [label for label, pattern in _COMPILED_TECH_TERMS.items() if pattern.search(text)]
