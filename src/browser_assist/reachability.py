"""
Link reachability check — flags an expired/dead job posting before the
user spends time on it. A plain HTTP HEAD (falling back to GET if HEAD
isn't allowed), respecting robots.txt first since this is an automated
fetch we make on the user's behalf, not a human clicking a link.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .robots import USER_AGENT, is_allowed_by_robots

logger = logging.getLogger("job_hunter.browser_assist.reachability")


@dataclass
class ReachabilityResult:
    reachable: bool
    status_code: int | None
    error: str | None = None
    robots_disallowed: bool = False


def check_url_reachable(url: str, *, timeout: int = 10) -> ReachabilityResult:
    if not is_allowed_by_robots(url):
        return ReachabilityResult(reachable=False, status_code=None, robots_disallowed=True, error="Disallowed by robots.txt")

    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        # Some sites don't implement HEAD properly (405/501) — fall back to GET.
        if resp.status_code in (405, 501):
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        return ReachabilityResult(reachable=resp.status_code < 400, status_code=resp.status_code)
    except requests.RequestException as exc:
        logger.warning("Reachability check failed for %s: %s", url, exc)
        return ReachabilityResult(reachable=False, status_code=None, error=str(exc))
