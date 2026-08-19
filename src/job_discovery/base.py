"""
Job discovery source adapter base.

Every adapter here talks to a genuinely public, officially-documented, free
API designed for third-party consumption (verified per-adapter — see each
module's own docstring for the specific evidence) — never scraping rendered
HTML, never bypassing CAPTCHA/anti-bot/login/robots.txt/rate limits. Each
adapter returns NormalizedJob objects, the exact same shape
src/sources/manual_import.py already produces, so nothing downstream
(insert_job -> the existing process_new_jobs pipeline) needs to know or
care where a job came from.

_polite_get() is the one HTTP entry point every adapter uses: it rate-
limits itself per-host (spec: "do not hammer websites") and always sends a
clear, identifying User-Agent rather than pretending to be a browser.
"""
from __future__ import annotations

import html
import logging
import re
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import requests

from src.sources.base import NormalizedJob

logger = logging.getLogger("job_hunter.job_discovery")

USER_AGENT = "ITJobHunterBot/1.0 (personal job-application assistant; respects robots.txt and rate limits)"

# Minimum seconds between requests to the SAME host, shared process-wide
# across every adapter instance — not per-adapter, so discovering from two
# different employers that happen to share a host still can't hammer it.
MIN_REQUEST_INTERVAL_SECONDS = 2.0
_last_request_at: dict[str, float] = {}

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_html(raw_html: str) -> str:
    """
    Minimal, dependency-free HTML-to-text: unescape entities, drop tags,
    collapse whitespace. Good enough for a job description body — this
    project doesn't need pixel-perfect formatting preservation, just
    readable plain text for the AI analysis/tailoring stages to read.

    Unescaping runs FIRST, before tag-stripping — found via live testing
    against the real Greenhouse API, whose `content` field comes back
    HTML-entity-encoded (literally "&lt;div&gt;", not "<div>"). Stripping
    tags before unescaping means the tag-matching regex runs on text with
    no literal "<...>" to find yet, and the real tags only appear *after*
    unescaping — by which point the old order had already finished
    stripping. Unescaping first handles both that case and the more common
    case of plain HTML with escaped entities inside the text (e.g. "&amp;")
    identically.
    """
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    text = text.replace("</p>", "\n\n").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = _TAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def polite_get(url: str, *, timeout: int = 15, **kwargs) -> requests.Response:
    """A GET that rate-limits itself per-host and sends an identifying User-Agent."""
    host = urlparse(url).netloc
    now = time.monotonic()
    elapsed = now - _last_request_at.get(host, 0.0)
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
    _last_request_at[host] = time.monotonic()

    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    return requests.get(url, timeout=timeout, headers=headers, **kwargs)


class JobDiscoverySource(ABC):
    """
    One adapter per platform's public job-board API. `identifier` is
    whatever token that platform's API needs to address one employer's
    board (a Greenhouse board token, a Lever company slug, etc.) — see
    config/employers.json for how these are configured.
    """

    platform: str

    @abstractmethod
    def discover(self, identifier: str) -> list[NormalizedJob]:
        """
        Fetch and normalize every open posting for one employer's board.
        Must never raise on a single malformed job entry — log and skip it,
        return everything else that parsed. May raise on a total fetch
        failure (network error, non-2xx response); the registry catches
        that per-employer so one broken/renamed board doesn't stop
        discovery for every other configured employer.
        """
        raise NotImplementedError
