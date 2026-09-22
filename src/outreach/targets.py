"""
Outreach target registry — the companies worth contacting directly.

config/outreach_companies.json is the ONLY place a company becomes an
outreach target. Nothing in this codebase invents, guesses, or search-scrapes
a company into this list: doing that reliably would need a paid company-data
or search API (which this project forbids) or scraping sites whose terms
prohibit it. You maintain the list; everything after it — research,
personalization, resume-grounded drafting — is automatic.

Mirrors src/job_discovery/registry.py's EmployerConfig/load_employers
deliberately: same file shape, same never-raise-on-one-bad-entry behaviour,
same "missing file is a warning, not an error" rule.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.config import PROJECT_ROOT
from src.job_discovery.registry import ADAPTERS

logger = logging.getLogger("job_hunter.outreach.targets")

OUTREACH_CONFIG_PATH = PROJECT_ROOT / "config" / "outreach_companies.json"


@dataclass
class OutreachTarget:
    """
    One company to research and potentially contact.

    Only `company` is required. A target with neither a website nor an ATS
    board will almost certainly end up with no postings found, because there
    would be nothing public for the system to legitimately learn about them.
    """

    company: str
    website: str = ""
    platform: str = ""
    identifier: str = ""
    # An address you know is right (e.g. published on their careers page).
    # Nothing is contacted without one — the pipeline never guesses.
    contact_email: str = ""
    notes: str = ""
    enabled: bool = True

    def has_job_board(self) -> bool:
        return bool(self.platform and self.identifier)

    def to_company_record(self) -> dict:
        """Shape expected by outreach_repo.upsert_company."""
        return {
            "name": self.company,
            "website": self.website,
            "contact_email": self.contact_email,
            "platform": self.platform,
            "identifier": self.identifier,
            "notes": self.notes,
            "source": "config",
        }


def load_targets(config_path: Path | None = None) -> list[OutreachTarget]:
    """
    Load and validate config/outreach_companies.json. Never raises on one
    malformed entry — logs and skips it, returns everything else that
    parsed. Returns an empty list (with a warning) if the file is missing or
    isn't a JSON array, so an unconfigured install simply has nothing to
    contact rather than crashing.
    """
    path = config_path or OUTREACH_CONFIG_PATH
    if not path.exists():
        logger.warning("Outreach registry not found at %s — no outreach targets configured.", path)
        return []

    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read outreach registry at %s: %s", path, exc)
        return []

    if not isinstance(raw, list):
        logger.error("Outreach registry at %s must be a JSON array of objects.", path)
        return []

    targets: list[OutreachTarget] = []
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning("Skipping malformed outreach entry %r: not an object", entry)
            continue

        company = str(entry.get("company") or "").strip()
        if not company:
            logger.warning("Skipping outreach entry with no company name: %r", entry)
            continue

        platform = str(entry.get("platform") or "").strip().lower()
        identifier = str(entry.get("identifier") or "").strip()
        if platform and platform not in ADAPTERS:
            logger.warning(
                "Outreach entry %r uses unsupported platform %r — ignoring that board. "
                "Supported: %s",
                company, platform, sorted(ADAPTERS),
            )
            platform, identifier = "", ""

        targets.append(
            OutreachTarget(
                company=company,
                website=str(entry.get("website") or "").strip(),
                platform=platform,
                identifier=identifier,
                contact_email=str(entry.get("contact_email") or "").strip().lower(),
                notes=str(entry.get("notes") or "").strip(),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return targets


def enabled_targets(config_path: Path | None = None) -> list[OutreachTarget]:
    return [target for target in load_targets(config_path) if target.enabled]


def _normalized_key(company: str, identifier: str, platform: str) -> tuple[str, str, str]:
    """
    Idempotency key for an outreach entry. Identity is (platform, identifier)
    when both are set — that's the strongest signal, and immune to the
    display-name variations Ishmam flagged ("SafetyCulture" vs "Safety
    Culture" would otherwise both get added). Falls back to a normalized
    company name only when no board is known, so an entry the user typed
    without a platform still deduplicates.
    """
    plat = (platform or "").strip().lower()
    ident = (identifier or "").strip().lower()
    if plat and ident:
        return (plat, ident, "")
    return ("", "", (company or "").strip().lower())


def add_target(
    entry: dict,
    *,
    config_path: Path | None = None,
) -> tuple[bool, str]:
    """
    Append one entry to config/outreach_companies.json, or report it
    already exists.

    Returns (added, reason) — `added` is True only when a new entry was
    written. When False, `reason` says why:

      - "already_present" — the target's platform/identifier (or, if
        neither is set, its normalized company name) matches an entry
        that's already there. NEVER duplicates, NEVER updates the
        existing entry.

    Idempotent by design: calling this twice in quick succession
    (double-click, retry, or the second half of a race) writes the
    entry once, and callers can present a calm "already added" flash
    the second time.
    """
    path = config_path or OUTREACH_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []
    else:
        existing = []
    if not isinstance(existing, list):
        existing = []

    company = str(entry.get("company") or "").strip()
    if not company:
        return (False, "no_company_name")

    incoming_key = _normalized_key(
        company,
        str(entry.get("identifier") or ""),
        str(entry.get("platform") or ""),
    )
    for row in existing:
        if not isinstance(row, dict):
            continue
        existing_key = _normalized_key(
            str(row.get("company") or ""),
            str(row.get("identifier") or ""),
            str(row.get("platform") or ""),
        )
        if existing_key == incoming_key:
            return (False, "already_present")

    existing.append(entry)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    return (True, "added")
