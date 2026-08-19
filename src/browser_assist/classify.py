"""
Application type classification (spec section 6) and informational ATS-
platform labeling.

classify_application_type() always returns TYPE_B (manual). This is
deliberate, not a placeholder: spec section 28 requires automation to stop
"unless explicitly configured otherwise AND the target site's policies
permit it" — the second half of that condition (verified per-site ToS
permission) cannot be determined reliably in code. Guessing wrong in either
direction is unacceptable: wrongly calling a site TYPE_A risks violating
its terms; there is no safe way to auto-detect "this specific site has
authorized automated form-filling." So every job goes to the manual queue,
which is always correct and always spec-compliant. If a future version adds
a genuinely verified-safe TYPE_A path for a specific, explicitly-reviewed
site, it plugs in here.

detect_platform() is separate and purely informational — a label like
"Workday" or "Greenhouse" shown in the dashboard so the user knows what
kind of form to expect, based only on the URL string (no page fetch, no
automation, nothing to bypass).
"""
from __future__ import annotations

from urllib.parse import urlparse

# Hostname substrings for common ATS/job-board platforms, for informational
# labeling only. Not exhaustive, and being unrecognized is not an error —
# it just means the platform isn't in this small reference list.
_PLATFORM_HOSTNAME_PATTERNS: dict[str, str] = {
    "myworkdayjobs.com": "Workday",
    "workday.com": "Workday",
    "greenhouse.io": "Greenhouse",
    "lever.co": "Lever",
    "smartrecruiters.com": "SmartRecruiters",
    "icims.com": "iCIMS",
    "successfactors.com": "SuccessFactors",
    "taleo.net": "Taleo",
    "seek.com.au": "SEEK",
    "indeed.com": "Indeed",
    "linkedin.com": "LinkedIn",
    "bamboohr.com": "BambooHR",
}


def classify_application_type(job_url: str) -> str:  # noqa: ARG001 — url kept for a future verified-safe extension point
    return "TYPE_B"


def detect_platform(job_url: str) -> str | None:
    """Best-effort, URL-only, informational platform label. Never fetches the page."""
    try:
        hostname = (urlparse(job_url).hostname or "").lower()
    except ValueError:
        return None
    for pattern, label in _PLATFORM_HOSTNAME_PATTERNS.items():
        if pattern in hostname:
            return label
    return None
