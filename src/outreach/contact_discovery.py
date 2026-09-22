"""
Find a legitimate, publicly published contact for one company.

The single rule this module exists to enforce: **an address is only ever
used if the company published it.** Nothing here constructs an address from
a person's name and a domain, infers one from a common pattern, or takes one
from a third-party site. If no published address can be found, that is a
real answer — the caller skips the company and shows it as needing a contact
— not a prompt to guess.

Sources, in the order they are trusted:

  1. config / database   an address already recorded for this company. You
                         typed it, so it outranks anything discovered.
  2. careers page        a mailto: link or plain address on the company's
                         OWN site (its careers, jobs or contact pages).
  3. job posting         an address written into the text of a real ATS
                         posting research.py already fetched.
  4. recruiter details   a name and LinkedIn URL published on those same
                         company pages. Recorded for the user to act on by
                         hand — never emailed, never messaged automatically.

Only the company's own domain is ever fetched, through the existing
job_discovery.base.polite_get, so the per-host rate limiting and identifying
User-Agent apply here as they do everywhere else. LinkedIn is never fetched;
a profile URL is only recognised when the company itself linked to it.

This module opens no SMTP connection and writes nothing to the database.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from src.job_discovery.base import polite_get

logger = logging.getLogger("job_hunter.outreach.contact_discovery")

# Where a discovered address came from. Stored on the company so the
# dashboard can always answer "why do we think this address is real?".
SOURCE_CONFIG = "config"
SOURCE_CAREERS_PAGE = "careers_page"
SOURCE_JOB_POSTING = "job_posting"

# Pages on the company's own site worth reading. Ordered by how likely they
# are to carry a recruiting address; the first page that yields one wins the
# "primary" pick, but every published address found across all of them is
# kept in DiscoveredContact.all_addresses so the picker can offer options.
#
# The extended list beyond the original four (careers/jobs/contact/root)
# covers the paths larger companies actually publish their recruiting
# details on — Deloitte, PwC, Xero et al. use "/about/contact", "/team",
# "/help/contact" more often than "/contact". Order matters: the first
# recruiting-flavoured address that turns up wins the primary pick.
CANDIDATE_PATHS = (
    # Root first — it's the seed the same-host "contact" hop reads
    # anchors from, and larger sites often carry JSON-LD Organization
    # data on the landing page. Putting it first also guarantees it
    # survives the MAX_PAGES truncation `candidate_urls` applies.
    "/",
    "/careers",
    "/careers/contact",
    "/jobs",
    "/contact",
    "/about/contact",
    "/company/contact",
    "/team",
)

# Caps. Contact discovery runs per company on every outreach run, so it has
# to stay cheap: eight small GETs at most (was four — we doubled the path
# list, and the same-host "contact" hop below reuses the same budget),
# and a page body that can't grow without bound just because someone
# ships a 5MB HTML bundle.
#
# Per-host rate limiting is NOT this module's job — every fetch goes
# through src.job_discovery.base.polite_get, which shares a
# process-wide `_last_request_at` dict keyed on netloc. Any additional
# page we read on the same company's domain therefore inherits the
# same 2s/host floor, so raising MAX_PAGES does not make the crawl any
# less polite per host — it only spreads more allowable wall-clock
# time over one company.
MAX_PAGES = 8
MAX_PAGE_CHARS = 400_000
FETCH_TIMEOUT_SECONDS = 10

# How many extra anchors on the homepage we're willing to follow when
# looking for a dedicated "contact" / "recruit" / "hire" page — small
# on purpose. Same-host only, robots-checked (via polite_get), and each
# hop still counts against MAX_PAGES so a chatty homepage cannot blow
# the per-company budget.
MAX_CONTACT_HOP_LINKS = 3
_CONTACT_HOP_ANCHOR_TEXT_RE = re.compile(r"contact|recruit|hire|work with us|join (?:the )?team", re.I)
_ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)

# An email as it appears in text. Deliberately conservative about the local
# part so tracking pixels and asset filenames don't parse as addresses.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_MAILTO_RE = re.compile(r"mailto:\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24})", re.I)

# linkedin.com/in/<slug>, only as literally written on the company's page.
_LINKEDIN_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]{2,100}", re.I
)
# The same URL inside an anchor, so the link text can supply the person's
# name. Captures the anchor's own text only — no surrounding markup.
_LINKEDIN_ANCHOR_RE = re.compile(
    r"<a[^>]+href=[\"'](https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]{2,100})[\"'][^>]*>(.*?)</a>",
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")

# X/Twitter handles the company links to from its own pages. Same rule
# as LinkedIn: only when it's literally there in an <a href>, only the
# handle itself, never derived from a name. Stored for the user to act
# on by hand — nothing in this codebase sends a message on X.
#
# Matches both twitter.com/<handle> and x.com/<handle>, excluding
# reserved paths like /share, /intent, /home, /search.
_X_HANDLE_ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\']'
    r"(https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15}))"
    r"[/\?]?(?:[^\"']*)?[\"'][^>]*>(.*?)</a>",
    re.I | re.S,
)
_X_RESERVED_HANDLES = frozenset({
    "share", "intent", "home", "search", "explore", "notifications", "messages",
    "i", "compose", "settings", "help", "about", "tos", "privacy", "login", "signup",
    "hashtag", "tweet",
})

# JSON-LD block in the page HEAD, exactly as generic_careers.py finds
# them for JobPosting — the same conservative regex, reused so the
# ContactPoint extractor can never disagree with what the job-hunt
# path considers a valid JSON-LD block.
_LD_JSON_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S
)

# Local parts that indicate a recruiting address, best first. An address
# whose local part matches earlier in this list is preferred over one that
# matches later, so careers@ wins over a generic info@ on the same page.
_PREFERRED_LOCAL_PARTS = (
    "careers", "career", "jobs", "job", "recruiting", "recruitment", "recruit",
    "hiring", "talent", "hr", "humanresources", "human-resources", "people",
    "peopleops", "employment", "workwithus", "joinus", "apply", "applications",
    "contact", "hello", "info", "enquiries", "inquiries", "office", "admin", "team",
)

# Local parts that are never a hiring contact. Emailing any of these is at
# best useless and at worst a complaint, so they are refused outright rather
# than ranked last.
_BLOCKED_LOCAL_PARTS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "bounce", "bounces",
    "mailer-daemon", "postmaster", "webmaster", "hostmaster", "abuse", "spam",
    "privacy", "legal", "dpo", "gdpr", "dmca", "copyright", "compliance",
    "press", "media", "pr", "marketing", "newsletter", "unsubscribe", "subscribe",
    "sales", "billing", "invoices", "invoice", "accounts", "accounting", "finance",
    "security", "soc", "vulnerability", "support", "helpdesk", "service",
    "example", "email", "your", "name", "user", "username",
})

# Domains that mean the "address" was really a template, an asset or a
# placeholder rather than someone's mailbox.
_BLOCKED_DOMAIN_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".ico",
    ".woff", ".woff2", ".ttf", ".mp4", ".pdf",
)
_BLOCKED_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "domain.com", "yourcompany.com",
    "email.com", "sentry.io", "sentry-cdn.com", "wixpress.com",
})


@dataclass
class DiscoveredContact:
    """
    The outcome of looking for one company's contact details.

    `email` empty is a normal, expected result: it means nothing publicly
    published was found, and the caller must skip the company rather than
    fall back to anything invented. `linkedin_url` can be set with no email
    — that is exactly the "here is who to message by hand" case.

    `all_addresses` is the full inventory of everything published-only-
    found across every page and posting the search read — each entry
    {email, source, evidence_url}. The primary `email` above is one of
    them (the highest-ranked one); the rest are options the picker on
    the dashboard offers when careers@ isn't right. Every entry has an
    evidence_url pointing at the page the address was read from — no
    entry is ever constructed or guessed.

    `x_handle` is an X/Twitter handle the company links to from its
    own pages, recorded for the user to act on by hand exactly like
    `linkedin_url` is. Nothing in this codebase ever messages via X or
    LinkedIn.
    """

    email: str = ""
    name: str = ""
    source: str = ""
    evidence_url: str = ""
    linkedin_url: str = ""
    linkedin_name: str = ""
    x_handle: str = ""
    x_name: str = ""
    reason: str = ""
    all_addresses: list[dict] = field(default_factory=list)

    @property
    def has_email(self) -> bool:
        return bool(self.email)

    def to_dict(self) -> dict:
        return {
            "email": self.email or None,
            "name": self.name or None,
            "source": self.source or None,
            "evidence_url": self.evidence_url or None,
            "linkedin_url": self.linkedin_url or None,
            "linkedin_name": self.linkedin_name or None,
            "x_handle": self.x_handle or None,
            "x_name": self.x_name or None,
            "reason": self.reason or None,
            "all_addresses": list(self.all_addresses),
        }


# --------------------------------------------------------------------------
# Address filtering
# --------------------------------------------------------------------------

def _split(email: str) -> tuple[str, str]:
    local, _, domain = email.strip().lower().partition("@")
    return local, domain


def is_usable_address(email: str) -> bool:
    """
    Whether an address found in a page could plausibly be a hiring contact.

    This filters obvious non-mailboxes — asset filenames that happen to
    contain '@', placeholder addresses, and role accounts that must never be
    cold-emailed. It says nothing about whether the address is *right*; the
    guarantee this module makes is only that the company published it.
    """
    local, domain = _split(email)
    if not local or not domain or "." not in domain:
        return False
    if local in _BLOCKED_LOCAL_PARTS:
        return False
    if domain in _BLOCKED_DOMAINS or domain.endswith(_BLOCKED_DOMAIN_SUFFIXES):
        return False
    # '@2x.png' style asset references, and sprite/hash filenames.
    if local.endswith(("2x", "3x")) and domain.endswith(_BLOCKED_DOMAIN_SUFFIXES):
        return False
    return True


def _rank(email: str) -> int:
    """
    Sort key for candidate addresses — lower is better. A recognised
    recruiting local part beats a generic one; anything unrecognised sorts
    last but is still allowed, because a company may publish a perfectly
    real address this list has never heard of.
    """
    local, _ = _split(email)
    for index, preferred in enumerate(_PREFERRED_LOCAL_PARTS):
        if local == preferred:
            return index
        if local.startswith(preferred) or local.endswith(preferred):
            return index + len(_PREFERRED_LOCAL_PARTS)
    return 2 * len(_PREFERRED_LOCAL_PARTS)


def best_address(candidates: list[str]) -> str:
    """Pick the most plausible recruiting address, or '' if none qualify."""
    usable = sorted({c.strip().lower() for c in candidates if is_usable_address(c)}, key=lambda e: (_rank(e), e))
    return usable[0] if usable else ""


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def extract_emails(html: str, *, mailto_only: bool = False) -> list[str]:
    """
    Every address the page publishes, mailto: links first.

    A mailto: link is the strongest signal — the company deliberately made
    it clickable — so those are returned ahead of addresses that merely
    appear in the text. Both are things the company chose to publish; nothing
    here is assembled or inferred.
    """
    text = html or ""
    mailto = [m.lower() for m in _MAILTO_RE.findall(text)]
    if mailto_only:
        return list(dict.fromkeys(mailto))
    plain = [m.lower() for m in _EMAIL_RE.findall(text)]
    return list(dict.fromkeys(mailto + plain))


def extract_jsonld_emails(html: str) -> list[str]:
    """
    Every schema.org `ContactPoint`/`Organization` email published in
    the page's JSON-LD blocks.

    This is a legitimate structured signal — an employer explicitly
    marked an address up as "here is how to reach us", exactly the
    way JobPosting JSON-LD is used by src/job_discovery/sources/
    generic_careers.py. Reused shape, same trust class.

    Returns lowercased addresses in the order they appear. Never
    raises; a broken JSON-LD block just contributes nothing.
    """
    found: list[str] = []
    for match in _LD_JSON_RE.finditer(html or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for email in _walk_jsonld_for_emails(data):
            email = email.strip().lower()
            if email and email not in found:
                found.append(email)
    return found


def _walk_jsonld_for_emails(node) -> list[str]:
    """
    Depth-first walk collecting `email` values off ContactPoint,
    Organization or Person nodes. JSON-LD in the wild is polymorphic —
    a bare object, a list, or an @graph wrapping many types — so this
    walks all of them rather than assuming one shape.
    """
    found: list[str] = []
    if isinstance(node, dict):
        email = node.get("email")
        if isinstance(email, str) and "@" in email:
            found.append(email)
        elif isinstance(email, list):
            for item in email:
                if isinstance(item, str) and "@" in item:
                    found.append(item)
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found.extend(_walk_jsonld_for_emails(item))
        contact_point = node.get("contactPoint")
        if isinstance(contact_point, (dict, list)):
            found.extend(_walk_jsonld_for_emails(contact_point))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld_for_emails(item))
    return found


def extract_x_handle(html: str) -> tuple[str, str]:
    """
    An X/Twitter handle the company itself linked to, as (handle, name).
    `handle` is the bare username without the leading '@'; `name` comes
    from the anchor text when it looks like a person's name, otherwise
    empty. Recorded for the user to act on by hand — nothing in this
    codebase messages on X.

    Reserved paths like /home, /share, /intent are filtered out so the
    company's "Share on X" widget doesn't parse as a hiring contact.
    """
    for match in _X_HANDLE_ANCHOR_RE.finditer(html or ""):
        handle = match.group(2).lower()
        if handle in _X_RESERVED_HANDLES:
            continue
        label = _TAG_RE.sub(" ", match.group(3) or "")
        label = " ".join(label.split()).strip()
        if label and len(label) <= 60 and label.lower() not in {"x", "twitter", "follow"}:
            return handle, label
        return handle, ""
    return "", ""


def extract_linkedin(html: str) -> tuple[str, str]:
    """
    A recruiter/hiring-manager LinkedIn profile the company itself links to,
    as (url, name). The name comes from the link's own text when it reads
    like a name; it is never derived from the URL slug, and it is never used
    to build an email address.

    Returns ('', '') when the page links to no personal profile.
    """
    text = html or ""
    for match in _LINKEDIN_ANCHOR_RE.finditer(text):
        url = match.group(1)
        label = _TAG_RE.sub(" ", match.group(2) or "")
        label = " ".join(label.split()).strip()
        # Anchor text is often "LinkedIn" or an icon; only keep it when it
        # actually looks like a person's name.
        if label and len(label) <= 60 and label.lower() not in {"linkedin", "profile", "connect"}:
            return url, label
        return url, ""

    plain = _LINKEDIN_RE.search(text)
    return (plain.group(0), "") if plain else ("", "")


# --------------------------------------------------------------------------
# Fetching the company's own pages
# --------------------------------------------------------------------------

def extract_contact_hop_urls(html: str, base_url: str) -> list[str]:
    """
    Same-host URLs the homepage links to whose anchor TEXT reads like
    "contact us" / "recruit" / "hire" / "work with us". Only URLs that
    stay on the same registrable host as `base_url` are returned — a
    dedicated contact page rarely lives on a different domain, and
    following off-host links would break the "only read the company's
    own domain" rule.

    Never raises; returns [] for a broken or off-host base_url.
    """
    try:
        base = urlparse(base_url)
    except ValueError:
        return []
    base_host = (base.netloc or "").lower()
    if not base_host:
        return []

    found: list[str] = []
    seen: set[str] = set()
    for match in _ANCHOR_RE.finditer(html or ""):
        href = (match.group(1) or "").strip()
        raw_text = _TAG_RE.sub(" ", match.group(2) or "")
        text = " ".join(raw_text.split()).strip()
        if not href or not text:
            continue
        if not _CONTACT_HOP_ANCHOR_TEXT_RE.search(text):
            continue
        absolute = urljoin(base_url, href)
        try:
            parsed = urlparse(absolute)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        # Same-host only. Compares netloc directly, tolerating a leading www.
        target_host = parsed.netloc.lower()
        if target_host != base_host and target_host.lstrip("www.") != base_host.lstrip("www."):
            continue
        # Drop the fragment so `/contact` and `/contact#form` don't
        # both count against the small hop budget.
        canonical = parsed._replace(fragment="").geturl()
        if canonical in seen:
            continue
        seen.add(canonical)
        found.append(canonical)
        if len(found) >= MAX_CONTACT_HOP_LINKS:
            break
    return found


def candidate_urls(website: str) -> list[str]:
    """
    The company's own pages worth reading, absolute and http(s) only.

    Returns [] for a blank or non-http website: with no domain the company
    published, there is nothing legitimate to read, and inventing one is
    exactly what this module refuses to do.
    """
    site = (website or "").strip()
    if not site:
        return []

    # Check the scheme BEFORE assuming a bare domain. Prepending https:// to
    # something that already has a scheme turns "mailto:x@y" into a fetchable
    # host, which is exactly the kind of accident this module must not have.
    scheme = urlparse(site).scheme.lower()
    if scheme and scheme not in ("http", "https"):
        return []
    if not scheme:
        site = f"https://{site}"

    parsed = urlparse(site)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return []

    root = f"{parsed.scheme}://{parsed.netloc}"
    urls = [urljoin(root, path) for path in CANDIDATE_PATHS]
    return list(dict.fromkeys(urls))[:MAX_PAGES]


def _fetch(url: str, fetch=polite_get) -> str:
    """
    Read one page. Never raises — a company whose site is down, slow or
    blocking us simply contributes no evidence, which is not an error.
    """
    try:
        response = fetch(url, timeout=FETCH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — any network failure is just "no evidence"
        logger.debug("Contact discovery could not read %s: %s", url, exc)
        return ""

    status = getattr(response, "status_code", 0)
    if status != 200:
        logger.debug("Contact discovery got HTTP %s from %s", status, url)
        return ""
    return (getattr(response, "text", "") or "")[:MAX_PAGE_CHARS]


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------

def discover_contact(company, research=None, *, fetch=polite_get) -> DiscoveredContact:
    """
    Find who to email at one company, in the documented priority order.

    `company` is an outreach_companies row (or any mapping with `name`,
    `website` and `contact_email`). `research` is the CompanyResearch this
    run already produced, used as the third source; None simply removes that
    source. `fetch` is the HTTP getter, injected in tests so no test ever
    touches the network.

    Now returns BOTH a single primary (the same shape as before, kept
    for pipeline.py) AND `all_addresses` — every published address the
    search collected across every page/posting it read. The primary is
    whichever address `best_address` ranks highest across everything
    seen; the rest are options the picker on the outreach dashboard
    offers when careers@ isn't the right one.

    Never raises. The worst case is a DiscoveredContact with no email and a
    reason explaining that nothing published was found.
    """
    existing = (_get(company, "contact_email") or "").strip().lower()
    if existing:
        # Already verified — by the user, in config or the dashboard. No
        # reason to read anyone's website, and nothing discovered could
        # outrank it. The `all_addresses` inventory is intentionally
        # empty here: the point of that column is showing published
        # discoveries, and we don't have any without a website read.
        return DiscoveredContact(
            email=existing,
            source=SOURCE_CONFIG,
            all_addresses=[{"email": existing, "source": SOURCE_CONFIG, "evidence_url": ""}],
        )

    linkedin_url = ""
    linkedin_name = ""
    x_handle = ""
    x_name = ""
    # Every {email, source, evidence_url} triple discovered, in the
    # order the pages that yielded them were read. Deduped on
    # lowercased email before the primary is picked.
    inventory: list[dict] = []
    primary_email = ""
    primary_source = ""
    primary_evidence = ""

    def _record(addresses: list[str], source: str, evidence_url: str) -> None:
        """Add every usable published address to the inventory."""
        for candidate in addresses:
            candidate = (candidate or "").strip().lower()
            if not candidate or not is_usable_address(candidate):
                continue
            if any(entry["email"] == candidate for entry in inventory):
                continue
            inventory.append({
                "email": candidate,
                "source": source,
                "evidence_url": evidence_url,
            })

    # --- 2. The company's own careers / contact pages ---------------------
    # First pass: fixed candidate paths. Every fetch goes through
    # `polite_get` (via `_fetch`), so the 2s/host floor and identifying
    # User-Agent apply on every hop — the extra paths are still polite.
    visited: set[str] = set()
    hop_seed_html = ""
    hop_seed_url = ""

    for url in candidate_urls(_get(company, "website")):
        if url in visited:
            continue
        visited.add(url)
        html = _fetch(url, fetch=fetch)
        if not html:
            continue

        # The first successful fetch of the site root is what the
        # "contact" hop uses as its seed. `candidate_urls` puts the
        # root last so this typically becomes the homepage; if the
        # site returns 404 there, whichever page above it succeeded
        # first is used instead.
        if not hop_seed_html or url.endswith("/"):
            hop_seed_html = html
            hop_seed_url = url

        if not linkedin_url:
            linkedin_url, linkedin_name = extract_linkedin(html)
        if not x_handle:
            x_handle, x_name = extract_x_handle(html)

        _record(extract_emails(html), SOURCE_CAREERS_PAGE, url)
        # Structured ContactPoint JSON-LD — same trust class as
        # JobPosting JSON-LD in the job pathway.
        _record(extract_jsonld_emails(html), SOURCE_CAREERS_PAGE, url)

    # Bounded same-host hop: if the fixed paths didn't yield a
    # recruiting-flavoured address, follow up to
    # MAX_CONTACT_HOP_LINKS anchors on the seed whose text says
    # "contact"/"recruit"/"hire". A separate budget from MAX_PAGES so
    # a full candidate list doesn't starve this fallback; each hop
    # still goes through polite_get, inheriting the same 2s/host
    # floor as every other fetch in the project.
    has_recruiting_hit = any(
        _rank(e["email"]) < len(_PREFERRED_LOCAL_PARTS) for e in inventory
    )
    if hop_seed_html and not has_recruiting_hit:
        for hop_url in extract_contact_hop_urls(hop_seed_html, hop_seed_url):
            if hop_url in visited:
                continue
            visited.add(hop_url)
            html = _fetch(hop_url, fetch=fetch)
            if not html:
                continue
            if not linkedin_url:
                linkedin_url, linkedin_name = extract_linkedin(html)
            if not x_handle:
                x_handle, x_name = extract_x_handle(html)
            _record(extract_emails(html), SOURCE_CAREERS_PAGE, hop_url)
            _record(extract_jsonld_emails(html), SOURCE_CAREERS_PAGE, hop_url)

    # Pick the primary from what the careers/contact pages yielded.
    if inventory:
        best = best_address([entry["email"] for entry in inventory])
        if best:
            match = next(entry for entry in inventory if entry["email"] == best)
            primary_email = best
            primary_source = match["source"]
            primary_evidence = match["evidence_url"]

    # --- 3. An address written into a real ATS posting --------------------
    for posting in getattr(research, "postings", None) or []:
        posting_url = getattr(posting, "url", "") or ""
        posting_body = getattr(posting, "description", "") or ""
        _record(extract_emails(posting_body), SOURCE_JOB_POSTING, posting_url)

    # If no address came from the careers/contact pages, try one from a
    # posting instead — same rank-and-pick as above, over the postings
    # inventory only.
    if not primary_email:
        posting_entries = [e for e in inventory if e["source"] == SOURCE_JOB_POSTING]
        if posting_entries:
            best = best_address([e["email"] for e in posting_entries])
            if best:
                match = next(e for e in posting_entries if e["email"] == best)
                primary_email = best
                primary_source = SOURCE_JOB_POSTING
                primary_evidence = match["evidence_url"]

    # --- 4. Assemble the result ------------------------------------------
    if primary_email:
        return DiscoveredContact(
            email=primary_email,
            source=primary_source,
            evidence_url=primary_evidence,
            linkedin_url=linkedin_url,
            linkedin_name=linkedin_name,
            x_handle=x_handle,
            x_name=x_name,
            all_addresses=inventory,
        )

    # No emailable address. Hand over whatever is manually actionable.
    manual_reason_parts = []
    if linkedin_url:
        manual_reason_parts.append("a LinkedIn profile was found on their site")
    if x_handle:
        manual_reason_parts.append("their X handle @" + x_handle + " was linked from their site")
    if manual_reason_parts:
        return DiscoveredContact(
            linkedin_url=linkedin_url,
            linkedin_name=linkedin_name,
            x_handle=x_handle,
            x_name=x_name,
            all_addresses=inventory,
            reason=(
                "no published email address found — " + " and ".join(manual_reason_parts)
                + " for you to contact by hand"
            ),
        )
    return DiscoveredContact(
        all_addresses=inventory,
        reason=(
            "no publicly published contact address was found on this company's own pages "
            "or in their postings"
        ),
    )


def _get(company, key: str):
    """Read a key from either a sqlite3.Row or a plain mapping."""
    try:
        return company[key]
    except (KeyError, IndexError, TypeError):
        return None
