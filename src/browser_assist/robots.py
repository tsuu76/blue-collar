"""
robots.txt compliance check (spec section 5/27: "Do not bypass... robots
restrictions"). Used before any AUTOMATED fetch this project makes of a
third-party page (e.g. src/browser_assist/reachability.py's link check) —
it does NOT apply to a human clicking a link in their own browser (the
dashboard's "Open job listing" link), only to code fetching on the user's
behalf without them directly driving it.
"""
from __future__ import annotations

import logging
from urllib import robotparser
from urllib.parse import urlparse

logger = logging.getLogger("job_hunter.browser_assist.robots")

USER_AGENT = "ITJobHunterBot/1.0 (personal job-application assistant; respects robots.txt)"


def is_allowed_by_robots(url: str, *, user_agent: str = USER_AGENT, timeout: int = 5) -> bool:
    """
    Returns True if robots.txt allows fetching this URL, or if robots.txt
    can't be found/parsed at all (fail open for missing robots.txt — its
    absence is not a disallow; fail closed only on an explicit Disallow
    rule). Never raises.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.read()
        return parser.can_fetch(user_agent, url)
    except Exception as exc:  # noqa: BLE001 — a broken robots.txt must never block us from proceeding
        logger.warning("Could not check robots.txt for %s: %s", url, exc)
        return True
