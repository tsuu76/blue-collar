"""
Tests for contact discovery.

Nothing here touches the network: every test injects a fake `fetch`, and one
test asserts that a company with no website never causes a fetch at all.

The point of most of these is negative — the module's value is what it
REFUSES to produce. Every address below uses a .invalid domain (RFC 2606),
so even a total failure of the safety rules could not reach a real mailbox.
"""
from __future__ import annotations

import pytest

from src.outreach.contact_discovery import (
    SOURCE_CAREERS_PAGE,
    SOURCE_CONFIG,
    SOURCE_JOB_POSTING,
    best_address,
    candidate_urls,
    discover_contact,
    extract_emails,
    extract_linkedin,
    is_usable_address,
)


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


class FakeFetcher:
    """Serves scripted pages by URL and records what was asked for."""

    def __init__(self, pages=None, error: Exception | None = None):
        self.pages = pages or {}
        self.error = error
        self.calls: list[str] = []

    def __call__(self, url, timeout=None, **kwargs):
        self.calls.append(url)
        if self.error:
            raise self.error
        if url not in self.pages:
            return FakeResponse(status_code=404)
        return FakeResponse(self.pages[url])


def company(**overrides) -> dict:
    return {
        "name": "Example Co",
        "website": "https://example.invalid",
        "contact_email": None,
        **overrides,
    }


class Posting:
    def __init__(self, description, url="https://boards.invalid/jobs/1"):
        self.description = description
        self.url = url


class Research:
    def __init__(self, postings):
        self.postings = postings


# --------------------------------------------------------------------------
# Address filtering — what may and may not be emailed
# --------------------------------------------------------------------------

class TestUsableAddresses:
    @pytest.mark.parametrize(
        "address",
        ["careers@example.invalid", "jobs@example.invalid", "recruitment@sub.example.invalid"],
    )
    def test_recruiting_addresses_are_usable(self, address):
        assert is_usable_address(address)

    @pytest.mark.parametrize(
        "address",
        [
            "noreply@example.invalid",
            "no-reply@example.invalid",
            "privacy@example.invalid",
            "legal@example.invalid",
            "press@example.invalid",
            "sales@example.invalid",
            "abuse@example.invalid",
            "postmaster@example.invalid",
            "unsubscribe@example.invalid",
        ],
    )
    def test_role_accounts_that_are_never_hiring_contacts_are_refused(self, address):
        assert not is_usable_address(address)

    @pytest.mark.parametrize(
        "address",
        ["logo@2x.png", "sprite@3x.jpg", "icon@2x.svg", "hello@example.com", "you@yourcompany.com"],
    )
    def test_asset_filenames_and_placeholders_are_refused(self, address):
        """
        Pages are full of things that look like addresses. A retina asset
        reference or a documentation placeholder must never be mistaken for
        somebody's mailbox.
        """
        assert not is_usable_address(address)

    def test_recruiting_addresses_outrank_generic_ones(self):
        assert best_address(
            ["info@example.invalid", "careers@example.invalid", "office@example.invalid"]
        ) == "careers@example.invalid"

    def test_a_generic_address_is_still_used_when_it_is_all_there_is(self):
        assert best_address(["info@example.invalid"]) == "info@example.invalid"

    def test_no_usable_candidates_means_no_address(self):
        assert best_address(["noreply@example.invalid", "logo@2x.png"]) == ""

    def test_an_unrecognised_local_part_is_allowed(self):
        """A company may publish an address this module has never heard of."""
        assert best_address(["talentacquisitionteam@example.invalid"]) == (
            "talentacquisitionteam@example.invalid"
        )


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

class TestExtraction:
    def test_mailto_links_come_first(self):
        html = """
          <p>General enquiries: office@example.invalid</p>
          <a href="mailto:careers@example.invalid">Email our team</a>
        """
        assert extract_emails(html)[0] == "careers@example.invalid"

    def test_plain_text_addresses_are_found_too(self):
        html = "<p>Send your CV to jobs@example.invalid</p>"
        assert "jobs@example.invalid" in extract_emails(html)

    def test_a_linkedin_profile_the_company_linked_to_is_found(self):
        html = '<a href="https://www.linkedin.com/in/example-recruiter">Alex Recruiter</a>'
        url, name = extract_linkedin(html)
        assert url == "https://www.linkedin.com/in/example-recruiter"
        assert name == "Alex Recruiter"

    def test_generic_link_text_is_not_treated_as_a_name(self):
        html = '<a href="https://linkedin.com/in/example-recruiter">LinkedIn</a>'
        url, name = extract_linkedin(html)
        assert url.endswith("/in/example-recruiter")
        assert name == ""

    def test_a_company_page_link_is_not_a_person(self):
        html = '<a href="https://www.linkedin.com/company/example-co">Follow us</a>'
        assert extract_linkedin(html) == ("", "")


class TestCandidateUrls:
    def test_only_the_companys_own_domain_is_read(self):
        urls = candidate_urls("https://example.invalid/some/page")
        assert all(u.startswith("https://example.invalid/") for u in urls)

    def test_a_bare_domain_is_accepted(self):
        assert "https://example.invalid/careers" in candidate_urls("example.invalid")

    def test_no_website_means_no_urls(self):
        assert candidate_urls("") == []
        assert candidate_urls(None) == []

    def test_non_http_schemes_are_refused(self):
        assert candidate_urls("mailto:someone@example.invalid") == []
        assert candidate_urls("file:///etc/passwd") == []


# --------------------------------------------------------------------------
# The priority order
# --------------------------------------------------------------------------

class TestPriorityOrder:
    def test_an_already_stored_address_wins_and_costs_no_network(self):
        fetch = FakeFetcher({"https://example.invalid/careers": "mailto:other@example.invalid"})
        contact = discover_contact(
            company(contact_email="typed-by-hand@example.invalid"), None, fetch=fetch
        )
        assert contact.email == "typed-by-hand@example.invalid"
        assert contact.source == SOURCE_CONFIG
        assert fetch.calls == []

    def test_a_careers_page_address_is_used_when_nothing_is_stored(self):
        fetch = FakeFetcher(
            {"https://example.invalid/careers": '<a href="mailto:careers@example.invalid">Jobs</a>'}
        )
        contact = discover_contact(company(), None, fetch=fetch)
        assert contact.email == "careers@example.invalid"
        assert contact.source == SOURCE_CAREERS_PAGE
        assert contact.evidence_url == "https://example.invalid/careers"

    def test_the_careers_page_beats_the_job_posting(self):
        fetch = FakeFetcher(
            {"https://example.invalid/careers": "mailto:careers@example.invalid"}
        )
        research = Research([Posting("Apply to hiring@example.invalid")])
        contact = discover_contact(company(), research, fetch=fetch)
        assert contact.source == SOURCE_CAREERS_PAGE

    def test_a_posting_address_is_used_when_the_site_has_none(self):
        fetch = FakeFetcher({"https://example.invalid/careers": "<p>No address here</p>"})
        research = Research([Posting("Send your CV to hiring@example.invalid")])
        contact = discover_contact(company(), research, fetch=fetch)
        assert contact.email == "hiring@example.invalid"
        assert contact.source == SOURCE_JOB_POSTING
        assert contact.evidence_url == "https://boards.invalid/jobs/1"


# --------------------------------------------------------------------------
# What it refuses to do — the reason this module exists
# --------------------------------------------------------------------------

class TestItNeverInvents:
    def test_no_published_address_means_no_address(self):
        fetch = FakeFetcher({"https://example.invalid/careers": "<p>Careers at Example</p>"})
        contact = discover_contact(company(), None, fetch=fetch)
        assert contact.email == ""
        assert not contact.has_email
        assert "no publicly published contact address" in contact.reason

    def test_a_recruiter_name_is_never_turned_into_an_address(self):
        """
        The hard rule. A page that names a hiring manager, with a domain
        sitting right there, must still produce no address — first.last@,
        f.last@ and every other pattern are guesses, and guesses are banned.
        """
        fetch = FakeFetcher(
            {
                "https://example.invalid/careers": (
                    "<p>Our hiring manager is Dana Example.</p>"
                    '<a href="https://www.linkedin.com/in/dana-example">Dana Example</a>'
                )
            }
        )
        contact = discover_contact(company(), None, fetch=fetch)
        assert contact.email == ""
        assert contact.linkedin_url == "https://www.linkedin.com/in/dana-example"
        assert contact.linkedin_name == "Dana Example"

    def test_a_linkedin_profile_is_never_an_email(self):
        fetch = FakeFetcher(
            {"https://example.invalid/careers": '<a href="https://linkedin.com/in/someone">x</a>'}
        )
        contact = discover_contact(company(), None, fetch=fetch)
        assert not contact.has_email
        assert "LinkedIn" in contact.reason

    def test_no_website_means_nothing_is_fetched(self):
        fetch = FakeFetcher()
        contact = discover_contact(company(website=""), None, fetch=fetch)
        assert fetch.calls == []
        assert not contact.has_email

    def test_third_party_sites_are_never_read(self):
        """Only the company's own domain is ever requested."""
        fetch = FakeFetcher()
        discover_contact(company(), None, fetch=fetch)
        assert fetch.calls
        for url in fetch.calls:
            assert url.startswith("https://example.invalid/")

    def test_a_blocked_address_does_not_become_the_contact(self):
        fetch = FakeFetcher(
            {"https://example.invalid/careers": "mailto:noreply@example.invalid"}
        )
        assert discover_contact(company(), None, fetch=fetch).email == ""


class TestFailureIsSafe:
    def test_a_network_error_is_not_an_exception(self):
        fetch = FakeFetcher(error=OSError("connection refused"))
        contact = discover_contact(company(), None, fetch=fetch)
        assert not contact.has_email

    def test_a_404_contributes_nothing(self):
        fetch = FakeFetcher({})
        assert not discover_contact(company(), None, fetch=fetch).has_email

    def test_later_pages_are_still_tried_after_an_empty_one(self):
        fetch = FakeFetcher({"https://example.invalid/contact": "mailto:careers@example.invalid"})
        contact = discover_contact(company(), None, fetch=fetch)
        assert contact.email == "careers@example.invalid"
        assert contact.evidence_url == "https://example.invalid/contact"

    def test_it_opens_no_smtp_connection(self):
        import pathlib

        source = pathlib.Path("src/outreach/contact_discovery.py").read_text()
        for token in ("smtplib", "sendmail", "SMTP("):
            assert token not in source


# --------------------------------------------------------------------------
# Widened coverage — extra paths, JSON-LD ContactPoint, X handle,
# same-host "contact" hop, and the all_addresses inventory.
# --------------------------------------------------------------------------

class TestExtendedCandidatePaths:
    """
    A recruiting address on a path beyond the original four
    (`/careers`, `/jobs`, `/contact`, `/`) is still found.
    """

    def test_finds_email_on_company_contact_path(self):
        pages = {
            "https://example.invalid/company/contact": (
                '<html><body>'
                '<a href="mailto:careers@example.invalid">Careers</a>'
                '</body></html>'
            ),
        }
        fetch = FakeFetcher(pages)
        result = discover_contact(company(), None, fetch=fetch)
        assert result.email == "careers@example.invalid"
        assert result.evidence_url == "https://example.invalid/company/contact"

    def test_finds_email_on_about_contact_path(self):
        pages = {
            "https://example.invalid/about/contact": (
                '<a href="mailto:hiring@example.invalid">Hiring</a>'
            ),
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        assert result.email == "hiring@example.invalid"


class TestJsonLdContactPoint:
    def test_extracts_email_from_organization_jsonld(self):
        pages = {
            "https://example.invalid/": (
                '<html><head>'
                '<script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Organization",'
                '"name":"Example","email":"careers@example.invalid"}'
                '</script></head><body></body></html>'
            ),
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        assert result.email == "careers@example.invalid"

    def test_extracts_email_from_contactpoint_jsonld(self):
        pages = {
            "https://example.invalid/contact": (
                '<script type="application/ld+json">'
                '{"@type":"Organization",'
                ' "contactPoint": {"@type":"ContactPoint",'
                '   "email":"jobs@example.invalid",'
                '   "contactType":"HR"}}'
                '</script>'
            ),
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        assert result.email == "jobs@example.invalid"

    def test_malformed_jsonld_is_silently_ignored(self):
        pages = {
            "https://example.invalid/contact": (
                '<script type="application/ld+json">{ this is not JSON</script>'
                '<a href="mailto:careers@example.invalid">Contact</a>'
            ),
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        # Broken JSON-LD doesn't crash the whole run; the mailto still wins.
        assert result.email == "careers@example.invalid"


class TestXHandleCapture:
    def test_records_x_handle_from_company_anchor(self):
        pages = {
            "https://example.invalid/": (
                '<a href="https://twitter.com/ExampleCareers">Careers on X</a>'
            ),
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        assert result.x_handle == "examplecareers"

    def test_ignores_x_share_widget_paths(self):
        # The company's own "Share on X" widget links to /share/... —
        # capturing "share" as a handle would be nonsense.
        pages = {
            "https://example.invalid/": (
                '<a href="https://twitter.com/share?text=Hi">Share on X</a>'
            ),
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        assert result.x_handle == ""

    def test_no_email_but_x_handle_is_surfaced_in_reason(self):
        pages = {"https://example.invalid/": '<a href="https://x.com/ExampleCareers">Careers</a>'}
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        assert result.email == ""
        assert result.x_handle == "examplecareers"
        assert "@examplecareers" in result.reason


class TestContactHop:
    """
    If none of the fixed paths yields a recruiting address, follow up
    to a small number of same-host anchors whose text says "contact" /
    "recruit" / "hire", and try each of THOSE for an address.
    """

    def test_follows_contact_hop_when_fixed_paths_miss(self):
        pages = {
            # Homepage carries no email but does link to a bespoke
            # contact page.
            "https://example.invalid/": (
                '<a href="/company/reach-out">Contact recruiting</a>'
                '<a href="/press">Press</a>'
            ),
            "https://example.invalid/company/reach-out": (
                '<a href="mailto:careers@example.invalid">Careers</a>'
            ),
        }
        fetch = FakeFetcher(pages)
        result = discover_contact(company(), None, fetch=fetch)
        assert result.email == "careers@example.invalid"
        assert "reach-out" in result.evidence_url

    def test_hop_only_follows_same_host(self):
        pages = {
            "https://example.invalid/": (
                '<a href="https://elsewhere.invalid/contact">Contact us elsewhere</a>'
            ),
        }
        fetch = FakeFetcher(pages)
        discover_contact(company(), None, fetch=fetch)
        # The off-host contact URL must never be fetched.
        assert "https://elsewhere.invalid/contact" not in fetch.calls

    def test_hop_ignores_javascript_anchors(self):
        pages = {
            "https://example.invalid/": (
                '<a href="javascript:openContact()">Contact us</a>'
            ),
        }
        fetch = FakeFetcher(pages)
        result = discover_contact(company(), None, fetch=fetch)
        assert result.email == ""
        # Nothing was enqueued off the javascript: anchor.
        assert not any(url.startswith("javascript:") for url in fetch.calls)


class TestAllAddressesInventory:
    def test_records_every_published_address(self):
        pages = {
            "https://example.invalid/careers": (
                '<a href="mailto:careers@example.invalid">Careers</a>'
                '<a href="mailto:jobs@example.invalid">Jobs</a>'
            ),
            "https://example.invalid/contact": (
                '<a href="mailto:hello@example.invalid">Contact</a>'
            ),
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        emails = {entry["email"] for entry in result.all_addresses}
        assert emails == {"careers@example.invalid", "jobs@example.invalid", "hello@example.invalid"}
        # Every entry carries its evidence URL — verifiability rule.
        for entry in result.all_addresses:
            assert entry["evidence_url"].startswith("https://example.invalid/")

    def test_inventory_dedupes_on_email(self):
        pages = {
            "https://example.invalid/careers": '<a href="mailto:careers@example.invalid">A</a>',
            "https://example.invalid/contact": '<a href="mailto:careers@example.invalid">B</a>',
        }
        result = discover_contact(company(), None, fetch=FakeFetcher(pages))
        assert len(result.all_addresses) == 1

    def test_existing_config_email_populates_inventory_as_config(self):
        # When the user already recorded an address in the DB, we
        # don't fetch anything — but the inventory still reflects
        # that one known address so the picker has something to show.
        fetch = FakeFetcher({})
        result = discover_contact(
            company(contact_email="known@example.invalid"), None, fetch=fetch,
        )
        assert result.email == "known@example.invalid"
        assert result.all_addresses == [
            {"email": "known@example.invalid", "source": SOURCE_CONFIG, "evidence_url": ""}
        ]
        # No fetching happened.
        assert fetch.calls == []

    def test_inventory_includes_posting_addresses_alongside_page_addresses(self):
        pages = {
            "https://example.invalid/careers": '<a href="mailto:careers@example.invalid">A</a>',
        }
        research = Research([
            Posting(description="Send resume to hiring@example.invalid",
                    url="https://boards.invalid/jobs/1"),
        ])
        result = discover_contact(company(), research=research, fetch=FakeFetcher(pages))
        by_source = {entry["email"]: entry["source"] for entry in result.all_addresses}
        assert by_source["careers@example.invalid"] == SOURCE_CAREERS_PAGE
        assert by_source["hiring@example.invalid"] == SOURCE_JOB_POSTING


class TestPolitenessInherited:
    """
    Every fetch goes through `polite_get`, which shares a process-wide
    per-host rate-limit dict. This test asserts that
    `discover_contact` never calls the network directly — the only way
    it reads a page is through the injected `fetch` argument, which in
    production is `polite_get`. So the 2s/host floor is inherited
    automatically, even for the new same-host contact hop.
    """

    def test_never_bypasses_the_fetch_argument(self):
        import pathlib

        source = pathlib.Path("src/outreach/contact_discovery.py").read_text()
        # No requests / urlopen / raw HTTP client references — the ONE
        # entry point is `polite_get`, taken as an argument default so
        # tests can inject their own fetcher.
        for banned in ("requests.get(", "urlopen(", "httpx.get(", "urllib.request"):
            assert banned not in source, f"contact_discovery must not use {banned!r} directly"

    def test_all_fetches_route_through_the_injected_fetch(self):
        pages = {
            "https://example.invalid/": '<a href="/reach">Contact</a>',
            "https://example.invalid/reach": '<a href="mailto:careers@example.invalid">A</a>',
        }
        fetch = FakeFetcher(pages)
        discover_contact(company(), None, fetch=fetch)
        # Every URL touched came through the FakeFetcher — proof
        # nothing bypasses the rate-limited entry point.
        assert fetch.calls, "at least one page should have been fetched"
        for url in fetch.calls:
            assert url.startswith("https://example.invalid/")
