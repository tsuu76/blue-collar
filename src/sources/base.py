"""
Job source abstraction.

Per the spec, automated scraping must never bypass CAPTCHA, Cloudflare,
login walls, rate limits, or robots restrictions — which rules out reliable
automated collection from SEEK/Indeed today. So the first-class, always-on
source is manual import (paste a URL/description), with the door left open
for future sources that a site's own terms genuinely permit (a public RSS
feed, an official API) to plug into the same normalized shape without
touching the rest of the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class JobSourceKind(str, Enum):
    MANUAL = "manual"   # pasted URL / description — always available, never blocked
    FEED = "feed"        # a source's own public RSS/Atom feed, where one genuinely exists
    API = "api"           # a source's own official public API, where one genuinely exists


@dataclass
class NormalizedJob:
    """The shape every job source must produce before it reaches dedupe/filtering."""

    source: str            # e.g. "manual_paste", "seek_rss", "employer_career_page"
    url: str
    title: str
    description: str
    company: str = ""
    location: str = ""
    salary: str = ""
    source_job_id: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "company": self.company,
            "location": self.location,
            "salary": self.salary,
            "source_job_id": self.source_job_id,
        }
