"""
Employer registry — config-driven list of employers to discover jobs from.

config/employers.json defines which employers to check and which adapter
(platform) + identifier to use for each. This is intentionally the ONLY
place a company name is configured anywhere in this project — the adapters
themselves are fully generic and work for any employer on that platform, so
adding coverage is a config-file edit, never a code change (see
config/README.md for how a user finds a company's identifier).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.config import PROJECT_ROOT
from src.sources.base import NormalizedJob

from .base import JobDiscoverySource
from .sources.ashby import AshbySource
from .sources.greenhouse import GreenhouseSource
from .sources.lever import LeverSource
from .sources.smartrecruiters import SmartRecruitersSource

logger = logging.getLogger("job_hunter.job_discovery.registry")

EMPLOYERS_CONFIG_PATH = PROJECT_ROOT / "config" / "employers.json"

ADAPTERS: dict[str, JobDiscoverySource] = {
    "greenhouse": GreenhouseSource(),
    "lever": LeverSource(),
    "smartrecruiters": SmartRecruitersSource(),
    "ashby": AshbySource(),
}


@dataclass
class EmployerConfig:
    company: str
    platform: str
    identifier: str
    enabled: bool = True


def load_employers(config_path: Path | None = None) -> list[EmployerConfig]:
    """
    Load and validate config/employers.json. Never raises on a malformed
    individual entry — logs and skips it, returns everything else that
    parsed. Returns an empty list (with a warning, not an error) if the
    file doesn't exist yet, so a fresh clone with no employers configured
    yet doesn't crash discovery — it just has nothing to check.
    """
    path = config_path or EMPLOYERS_CONFIG_PATH
    if not path.exists():
        logger.warning("Employer registry not found at %s — no automated discovery configured.", path)
        return []

    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read employer registry at %s: %s", path, exc)
        return []

    employers: list[EmployerConfig] = []
    for entry in raw:
        try:
            employers.append(
                EmployerConfig(
                    company=entry["company"],
                    platform=entry["platform"],
                    identifier=entry["identifier"],
                    enabled=entry.get("enabled", True),
                )
            )
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed employer entry %r: %s", entry, exc)
    return employers


def discover_from_employer(employer: EmployerConfig) -> list[NormalizedJob]:
    """
    Fetch jobs for one employer. Never raises — a fetch failure (network
    error, unknown platform, renamed/removed board) is logged and results
    in an empty list, so one broken employer entry never stops discovery
    for every other configured one.
    """
    adapter = ADAPTERS.get(employer.platform)
    if adapter is None:
        logger.warning(
            "Employer %r configured with unsupported platform %r — skipping. Supported: %s",
            employer.company,
            employer.platform,
            sorted(ADAPTERS),
        )
        return []
    try:
        return adapter.discover(employer.identifier)
    except Exception as exc:  # noqa: BLE001 — a single employer's fetch failure must not stop the rest
        logger.warning("Discovery failed for %r (%s/%s): %s", employer.company, employer.platform, employer.identifier, exc)
        return []
