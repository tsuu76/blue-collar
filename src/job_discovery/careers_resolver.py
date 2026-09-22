"""
Careers-page resolver — follow a company's OWN links to the careers system
it actually uses, and fingerprint that system from what the HTML really
contains.

The problem this solves: probing a fixed list of conventional paths
(/careers, /jobs, ...) on a company's root domain finds a *marketing* page
for most large Australian employers, not their job data. Woolworths, BHP,
Qantas, Monash and the ATO all publish real, machine-readable openings —
just not at any conventional path on their primary domain. Their homepage
links to a careers site, and that site is (almost always) a hosted ATS with
a public job endpoint.

So this module does the small, bounded crawl that closes the gap:

    homepage -> "Careers" link -> careers subdomain -> ATS fingerprint
             -> a tenant/site identifier an adapter can actually query

Three rules govern everything here, and they are the whole safety story:

  1. **Nothing is ever constructed from a company name.** An ATS tenant,
     board slug, site name or endpoint URL is only ever *extracted from a
     URL that genuinely appeared in that company's own HTML* — an anchor
     href, a script src, an iframe src, or a URL literal inside inline
     JavaScript. If the company's pages never mention a Workday tenant,
     this module never invents one, and the company stays "careers page
     verified, job data unreadable" rather than being pointed at a guessed
     board. (Conventional PATHS on an already-verified domain are still
     tried, as they always were — that is a guess about site layout, never
     about the company's identity.)
  2. **Every fetch is robots.txt-checked and rate-limited**, through the
     same `polite_get` the rest of job_discovery uses. The crawl is capped
     at MAX_PAGE_FETCHES pages per company and MAX_LINK_DEPTH hops from the
     homepage, so a badly-linked site costs a fixed, small number of
     requests rather than an open-ended walk.
  3. **Evidence is preserved.** Every resolution carries an `evidence` list
     naming each hop that led to the result, so a registry entry can always
     be traced back to the page it came from.

This module resolves and fingerprints; it never reads job data. Extracting
postings is the adapters' job (src/job_discovery/sources/), which is why
nothing here fetches an ATS *endpoint* — only, at most, an ATS careers page
that the company itself linked to.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from .base import polite_get

logger = logging.getLogger("job_hunter.job_discovery.careers_resolver")

# How many PAGES may be fetched while resolving one company. robots.txt
# fetches are not counted here: they are cached per-host for the life of the
# process and there is at most one per distinct host touched (three or four
# in the worst case), so the real ceiling is roughly this number plus a
# handful.
MAX_PAGE_FETCHES = 6

# Hops from the homepage. 1 = links found on the homepage; 2 = links found
# on the page those led to (the common "Careers landing page -> Search our
# jobs" pattern). Deeper than that stops being a careers lookup and starts
# being a site crawl.
MAX_LINK_DEPTH = 2

MAX_PAGE_CHARS = 600_000
FETCH_TIMEOUT_SECONDS = 10

# Conventional paths on a domain already believed real. A guess about where
# a page sits on a known site — never a guess about who the company is.
CONVENTIONAL_PATHS = ("/careers", "/jobs", "/about/careers", "/careers/jobs", "/about-us/careers")

# Careers-shaped hostnames to try on a verified domain when its own pages
# don't link anywhere useful. Same character as CONVENTIONAL_PATHS: a
# layout convention on a domain we already know is this company's.
CAREERS_SUBDOMAIN_PREFIXES = ("careers", "jobs")

# Anchor text / href fragments that mark a link as pointing at hiring
# content. Matched case-insensitively against both the visible text and the
# href, because plenty of sites use an icon-only or image-only link.
_CAREERS_WORDS = (
    "career", "careers", "jobs", "job-search", "vacancy", "vacancies",
    "work with us", "work-with-us", "work for us", "work-for-us",
    "join us", "join-us", "join our team", "employment", "opportunities",
    "current openings", "current-openings", "positions", "recruitment",
)

# Link text that looks careers-ish but reliably is not the jobs listing.
_CAREERS_NEGATIVE_WORDS = (
    "job seeker support", "jobkeeper", "jobseeker", "job-keeper",
    "career advice", "news", "media-release", "blog/",
)

_ANCHOR_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_ABSOLUTE_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")

_NOT_FOUND_MARKERS = (
    "page not found", "404 error", "we couldn't find that page", "this page doesn't exist",
    "oops! that page", "page you requested could not be found",
)

# Two-label public suffixes this project actually meets. Used only to decide
# whether a discovered link still belongs to the same organisation, so a
# missing entry here fails in the safe direction (the link is treated as
# third-party and not followed).
_TWO_LABEL_SUFFIXES = (
    "com.au", "net.au", "org.au", "gov.au", "edu.au", "asn.au", "id.au",
    "co.nz", "org.nz", "govt.nz", "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.sg", "com.my", "co.jp", "co.za",
)


# --------------------------------------------------------------------------
# ATS fingerprints
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AtsMatch:
    """One ATS system recognised in a URL that the company's own HTML
    contained. `platform` is the registry adapter key when this project can
    query the system ("workday", "greenhouse", ...) and "" when it can only
    name it; `label` is always the human name for reporting."""

    label: str
    platform: str
    identifier: str
    source_url: str


def _match_workday(parsed) -> tuple[str, str] | None:
    """
    Workday career sites look like:

        https://tenant.wd3.myworkdayjobs.com/en-US/Site_Name
        https://tenant.wd3.myworkdayjobs.com/Site_Name/job/Sydney/Analyst_R-1
        https://tenant.wd3.myworkday.com/wday/cxs/tenant/Site_Name/jobs

    The tenant is the first host label, the numbered `wdN` is which Workday
    cluster hosts them, and the first non-locale path segment is the career
    site name. All three come out of the URL itself — nothing is assembled
    from the company name.
    """
    host = parsed.netloc.lower()
    host_match = re.match(r"^([a-z0-9][a-z0-9-]*)\.(wd\d+)\.(myworkdayjobs\.com|myworkday\.com)$", host)
    if not host_match:
        return None
    tenant = host_match.group(1)

    segments = [s for s in parsed.path.split("/") if s]
    # /wday/cxs/{tenant}/{site}/... — the endpoint form, sometimes present
    # in inline JS on a company's own careers page.
    if len(segments) >= 4 and segments[0] == "wday" and segments[1] == "cxs":
        return tenant, segments[3]

    site = ""
    for segment in segments:
        if re.fullmatch(r"[a-z]{2}([-_][A-Za-z]{2})?", segment):
            continue  # locale prefix: en-US, en, fr_CA
        if segment.lower() in ("wday", "job", "jobs", "d", "recruiting"):
            continue
        if "." in segment:
            continue  # an asset path (widget.js, main.css), not a career site
        site = segment
        break
    if not site:
        return None
    return tenant, site


def _first_path_segment(parsed) -> str:
    segments = [s for s in parsed.path.split("/") if s]
    return segments[0] if segments else ""


def _match_ats(url: str) -> AtsMatch | None:
    """
    Recognise a hosted ATS from one absolute URL, extracting whatever
    identifier that platform's adapter needs. Returns None for anything
    unrecognised — including a recognised vendor whose URL carries no usable
    identifier, because a platform name with nothing to query it by is not
    something to write into the registry as an ATS entry.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if ":" in host:
        host = host.split(":", 1)[0]

    workday = _match_workday(parsed)
    if workday:
        tenant, site = workday
        return AtsMatch("Workday", "workday", f"https://{host}/{tenant}/{site}", url)
    if re.match(r"^[a-z0-9][a-z0-9-]*\.wd\d+\.(myworkdayjobs\.com|myworkday\.com)$", host):
        # Unmistakably Workday — a script or asset URL on a Workday host —
        # but with no career-site name in it. Named, not queried: an
        # identifier this project cannot read off the page is one it will
        # not invent.
        return AtsMatch("Workday", "", url, url)

    # Boards this project already has a working adapter for. The slug is
    # read out of the company's own link, which is exactly what makes it a
    # verified identifier rather than the name-derived guess that
    # src/outreach/discovery.py's slug_variants() produces.
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io"):
        slug = _first_path_segment(parsed)
        if slug == "embed":
            slug = ""
        if slug:
            return AtsMatch("Greenhouse", "greenhouse", slug, url)
    if host.endswith("greenhouse.io") and "for=" in (parsed.query or ""):
        for part in parsed.query.split("&"):
            if part.startswith("for="):
                slug = part[4:].strip()
                if slug:
                    return AtsMatch("Greenhouse", "greenhouse", slug, url)
    if host == "jobs.lever.co":
        slug = _first_path_segment(parsed)
        if slug:
            return AtsMatch("Lever", "lever", slug, url)
    if host in ("jobs.ashbyhq.com", "app.ashbyhq.com"):
        slug = _first_path_segment(parsed)
        if slug:
            return AtsMatch("Ashby", "ashby", slug, url)
    if host in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com"):
        slug = _first_path_segment(parsed)
        if slug:
            return AtsMatch("SmartRecruiters", "smartrecruiters", slug, url)

    # Systems this project can name but cannot yet query. The identifier
    # kept is the discovered URL itself: honest, traceable, and directly
    # usable by hand.
    detected_only = (
        ("PageUp", ("pageuppeople.com", "pageuphr.com", "pageuppeople.co")),
        ("SuccessFactors", ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu")),
        ("Taleo", ("taleo.net",)),
        ("JobAdder", ("jobadder.com", "applynow.net.au")),
        ("iCIMS", ("icims.com",)),
        ("Cornerstone", ("csod.com",)),
        ("Oracle Cloud Recruiting", ("oraclecloud.com/hcmUI/CandidateExperience".lower(),)),
        ("Workable", ("workable.com",)),
        ("Recruitee", ("recruitee.com",)),
        ("Elmo", ("elmotalent.com.au",)),
        ("Springboard", ("springboardrecruitment.com.au",)),
        ("LiveHire", ("livehire.com",)),
        ("Snaphire", ("snaphire.com",)),
    )
    lowered_url = url.lower()
    for label, needles in detected_only:
        for needle in needles:
            if needle in host or needle in lowered_url:
                return AtsMatch(label, "", url, url)
    return None


def detect_ats_in_html(html: str, page_url: str) -> AtsMatch | None:
    """
    The first ATS recognised among every absolute URL the page contains.

    Scanning raw absolute URLs rather than only parsed anchors is
    deliberate: careers widgets are embedded as often through a `<script
    src>`, an `<iframe src>`, or a URL string inside inline JavaScript as
    through a plain link, and all three forms are equally "what this
    company's own page says its careers system is".

    Adapter-backed platforms win over merely-named ones when a page mentions
    both (a site can embed a Workday widget and still footer-link an old
    Taleo site), because a platform this project can actually query yields
    real postings.
    """
    best: AtsMatch | None = None
    for raw in _ABSOLUTE_URL_RE.findall(html or ""):
        candidate = _match_ats(raw.rstrip("\"'\\),;"))
        if candidate is None:
            continue
        if candidate.platform:
            return candidate
        if best is None:
            best = candidate
    # A relative link can't name a third-party ATS, so only absolute URLs
    # matter here; page_url is kept for logging context only.
    if best is not None:
        logger.debug("Detected %s (no adapter) on %s", best.label, page_url)
    return best


# --------------------------------------------------------------------------
# robots.txt (shared by every module that fetches a company's own pages)
# --------------------------------------------------------------------------

_robots_cache: dict[str, RobotFileParser | None] = {}


def _robots_parser_for(url: str, *, fetch=polite_get) -> RobotFileParser | None:
    """
    One RobotFileParser per host, cached for the life of the process.
    Fetched through the same rate-limited `fetch` every other request uses,
    so a robots.txt read costs the same politeness budget as any page.

    Returns None when robots.txt itself could not be read, which is the
    standard convention for "no restrictions stated".
    """
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root in _robots_cache:
        return _robots_cache[root]

    parser: RobotFileParser | None = RobotFileParser()
    try:
        response = fetch(f"{root}/robots.txt", timeout=FETCH_TIMEOUT_SECONDS)
        if getattr(response, "status_code", 0) == 200:
            parser.parse((response.text or "").splitlines())
        else:
            parser = None
    except Exception as exc:  # noqa: BLE001 — unreachable robots.txt is not a failure
        logger.debug("Could not read robots.txt for %s: %s", root, exc)
        parser = None

    _robots_cache[root] = parser
    return parser


def robots_allows(url: str, *, fetch=polite_get) -> bool:
    """Whether this project's User-Agent may fetch `url`, per that host's own
    robots.txt. Fails open only when robots.txt could not be read — never
    when it was read and says no."""
    parser = _robots_parser_for(url, fetch=fetch)
    if parser is None:
        return True
    try:
        return parser.can_fetch("ITJobHunterBot", url) and parser.can_fetch("*", url)
    except Exception:  # noqa: BLE001 — a malformed robots.txt must not crash discovery
        return True


# --------------------------------------------------------------------------
# Link discovery
# --------------------------------------------------------------------------

def registrable_domain(host: str) -> str:
    """
    The organisation-level domain of a host: "careers.woolworths.com.au" ->
    "woolworths.com.au". Used only to decide whether a discovered link still
    belongs to the same company, so an unknown multi-label suffix errs
    toward treating the link as third-party and not following it.
    """
    host = (host or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        host = host.split(":", 1)[0]
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    suffix_two = ".".join(labels[-2:])
    if suffix_two in _TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return suffix_two


def _looks_careersy(text: str, href: str) -> bool:
    haystack = f"{text} {href}".lower()
    if any(bad in haystack for bad in _CAREERS_NEGATIVE_WORDS):
        return False
    return any(word in haystack for word in _CAREERS_WORDS)


def find_careers_links(html: str, page_url: str, *, own_domain: str) -> list[str]:
    """
    Careers-shaped links on this page, best first.

    "Best" means: a link straight to a recognised ATS, then a link to a
    careers-shaped hostname, then an ordinary in-site careers path. Only
    links belonging to this company (same registrable domain) or to a
    recognised ATS are returned — a careers link pointing at a job board
    this project does not recognise, or at any third-party site, is dropped
    rather than followed.
    """
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for href, inner in _ANCHOR_RE.findall(html or ""):
        text = _TAG_RE.sub(" ", inner or "")
        if not _looks_careersy(text, href):
            continue
        absolute = urljoin(page_url, href.strip())
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        absolute = absolute.split("#", 1)[0]
        if absolute in seen:
            continue

        ats = _match_ats(absolute)
        same_org = registrable_domain(parsed.netloc) == own_domain
        if ats is None and not same_org:
            continue

        host_label = parsed.netloc.lower().split(".", 1)[0]
        if ats is not None:
            score = 0
        elif host_label in CAREERS_SUBDOMAIN_PREFIXES:
            score = 1
        else:
            score = 2
        seen.add(absolute)
        scored.append((score, absolute))

    scored.sort(key=lambda pair: pair[0])
    return [url for _, url in scored]


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

@dataclass
class CareersResolution:
    """
    What a bounded crawl of one company's own site actually established.

    `careers_url` is the best real careers page reached, and `careers_html`
    is that page's body, kept so the caller can look for structured job data
    without re-fetching it. `platform` / `identifier` are filled only when an
    ATS was recognised in that company's own HTML — `platform` further only
    when this project has an adapter that can query it.
    """

    website: str
    careers_url: str = ""
    careers_html: str = ""
    platform: str = ""
    identifier: str = ""
    ats_label: str = ""
    ats_url: str = ""
    evidence: list[str] = field(default_factory=list)
    fetches: int = 0

    @property
    def reachable(self) -> bool:
        return bool(self.careers_url)

    @property
    def has_queryable_ats(self) -> bool:
        return bool(self.platform and self.identifier)


def _normalize_website(website: str) -> str:
    site = (website or "").strip()
    if not site:
        return ""
    scheme = urlparse(site).scheme.lower()
    if scheme and scheme not in ("http", "https"):
        return ""
    if not scheme:
        site = f"https://{site}"
    parsed = urlparse(site)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_a_real_page(status_code: int, html: str) -> bool:
    if status_code != 200 or not html:
        return False
    lowered = html.lower()
    return not any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def _is_careers_shaped(url: str, html: str) -> bool:
    """
    Whether a reachable page is plausibly about hiring, rather than just the
    homepage that happened to answer. Deliberately generous — this only
    decides which reachable page is *reported* as the careers URL, never
    whether a posting exists.
    """
    if any(word in url.lower() for word in ("career", "job", "vacanc", "employment", "recruit")):
        return True
    lowered = (html or "")[:MAX_PAGE_CHARS].lower()
    return any(word in lowered for word in ("current vacancies", "job search", "search jobs", "open positions"))


def resolve_careers(
    website: str,
    *,
    fetch=polite_get,
    max_fetches: int = MAX_PAGE_FETCHES,
    stop_when=None,
) -> CareersResolution:
    """
    Walk a company's own site to its real careers system, within a fixed
    budget.

    The queue is seeded with the homepage, then conventional paths, then
    careers-shaped subdomains, and grows as careers links are found on
    pages already fetched (up to MAX_LINK_DEPTH hops). Resolution stops
    early the moment an ATS is recognised — that is the answer this crawl
    exists to find, and further fetching would be waste.

    `stop_when(url, html)` lets the caller end the crawl on its own
    criterion without this module having to know what that criterion is —
    generic_careers.py passes "this page has JobPosting structured data on
    it", which is just as conclusive as an ATS hit and equally pointless to
    keep crawling past. Keeping the test out here is what stops this module
    from growing a second, parallel notion of what a job posting is.

    Never raises: an unreachable site, a redirect loop, a malformed page and
    a robots.txt refusal all come back as a resolution with nothing found.
    """
    root = _normalize_website(website)
    resolution = CareersResolution(website=website)
    if not root:
        resolution.evidence.append(f"no usable http(s) website for {website!r}")
        return resolution

    own_domain = registrable_domain(urlparse(root).netloc)

    # (priority, depth, url) — lower priority number is tried first.
    queue: list[tuple[int, int, str]] = [(0, 0, f"{root}/")]
    queue += [(3, 0, urljoin(root, path)) for path in CONVENTIONAL_PATHS[:2]]
    queue += [
        (4, 0, f"https://{prefix}.{own_domain}/")
        for prefix in CAREERS_SUBDOMAIN_PREFIXES
    ]
    queue += [(5, 0, urljoin(root, path)) for path in CONVENTIONAL_PATHS[2:]]

    visited: set[str] = set()
    best_page: tuple[int, str, str] | None = None  # (rank, url, html)

    while queue and resolution.fetches < max_fetches:
        queue.sort(key=lambda item: (item[0], item[1]))
        priority, depth, url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        if not robots_allows(url, fetch=fetch):
            resolution.evidence.append(f"robots.txt disallows {url}")
            continue

        try:
            response = fetch(url, timeout=FETCH_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — an unreachable page is a normal outcome
            logger.debug("Could not read %s: %s", url, exc)
            continue
        resolution.fetches += 1

        status = getattr(response, "status_code", 0)
        html = (getattr(response, "text", "") or "")[:MAX_PAGE_CHARS]
        if not _looks_like_a_real_page(status, html):
            continue

        ats = detect_ats_in_html(html, url)
        if ats is not None:
            resolution.platform = ats.platform
            resolution.identifier = ats.identifier if ats.platform else ""
            resolution.ats_label = ats.label
            resolution.ats_url = ats.source_url
            resolution.careers_url = url
            resolution.careers_html = html
            resolution.evidence.append(f"{ats.label} referenced by {url} ({ats.source_url})")
            return resolution

        if stop_when is not None and stop_when(url, html):
            resolution.careers_url = url
            resolution.careers_html = html
            resolution.evidence.append(f"structured job data on {url}")
            return resolution

        rank = 0 if _is_careers_shaped(url, html) else 1
        if best_page is None or rank < best_page[0]:
            best_page = (rank, url, html)
            resolution.evidence.append(f"reachable page {url}")

        if depth < MAX_LINK_DEPTH:
            for link in find_careers_links(html, url, own_domain=own_domain)[:3]:
                if link not in visited:
                    queue.append((1, depth + 1, link))

    if best_page is not None:
        _, url, html = best_page
        resolution.careers_url = url
        resolution.careers_html = html
    else:
        resolution.evidence.append("no careers/jobs page could be reached on this domain")
    return resolution
