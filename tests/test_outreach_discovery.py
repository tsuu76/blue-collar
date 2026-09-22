"""
Tests for automated company discovery.

No network: every test injects fake adapters, so no real ATS API is called.
All company names here are synthetic fixtures.

The property that matters throughout: a candidate name asserts nothing, and
only a board that a real adapter confirms with real postings is accepted.
"""
from __future__ import annotations

import json

import pytest

from src.job_discovery.base import JobDiscoverySource
from src.outreach.discovery import (
    Candidate,
    VerifiedBoard,
    deduplicate,
    discover_company,
    load_candidates,
    merge_into_outreach_config,
    refresh_careers_page_entries,
    run_discovery,
    slug_variants,
    verify_board,
    verify_careers_page,
)
from src.sources.base import NormalizedJob

CAREERS_PAGE_WITH_JOBS = """
<html><body>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Service Desk Analyst",
 "description": "Troubleshoot issues. Python and SQL used daily.",
 "url": "https://example.invalid/careers/1"}
</script>
</body></html>
"""

CAREERS_PAGE_NO_STRUCTURED_DATA = """
<html><body>
<h1>Careers at Example Co</h1>
<p>We use Workday to manage our openings.</p>
<script src="https://example.wd1.myworkdayjobs.com/widget.js"></script>
</body></html>
"""


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def _fetcher(pages):
    def _fetch(url, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return FakeResponse(status_code=404)
        if url not in pages:
            return FakeResponse(status_code=404)
        return FakeResponse(pages[url])

    return _fetch


# "Service Desk Analyst" is a title the project's EXISTING entry-level
# filter (src/job_filter/cheap_filter.py) actually accepts. That filter is
# strict on purpose — "Junior Support Engineer", for instance, is rejected
# as NO_POSITIVE_MATCH — so fixtures here use titles it really passes.
def _posting(title="Service Desk Analyst", **kw) -> NormalizedJob:
    return NormalizedJob(
        **{
            "source": "greenhouse",
            "url": "https://example.invalid/1",
            "title": title,
            "description": "Troubleshoot issues. Python and SQL used daily.",
            "company": "Example Co",
            "location": "Sydney",
            "source_job_id": "1",
            **kw,
        }
    )


class FakeAdapter(JobDiscoverySource):
    """
    Resolves only for identifiers it was told about — every other slug guess
    raises, exactly as a real 404 would.
    """

    platform = "greenhouse"

    def __init__(self, boards: dict[str, list[NormalizedJob]] | None = None):
        self.boards = boards or {}
        self.calls: list[str] = []

    def discover(self, identifier: str) -> list[NormalizedJob]:
        self.calls.append(identifier)
        if identifier not in self.boards:
            raise RuntimeError("404 Client Error: Not Found")
        return list(self.boards[identifier])


@pytest.fixture()
def adapters():
    return {"greenhouse": FakeAdapter({"exampleco": [_posting()]})}


@pytest.fixture()
def write_candidates(tmp_path):
    def _write(entries):
        path = tmp_path / "candidate_companies.json"
        path.write_text(json.dumps(entries))
        return path

    return _write


@pytest.fixture()
def outreach_config(tmp_path):
    path = tmp_path / "outreach_companies.json"
    path.write_text("[]")
    return path


class TestSlugVariants:
    def test_generates_sensible_slugs(self):
        assert slug_variants("Culture Amp") == ["cultureamp", "culture-amp", "culture"]

    def test_single_word_collapses_to_one(self):
        assert slug_variants("Canva") == ["canva"]

    def test_strips_punctuation(self):
        assert slug_variants("Simply Wall St.") == ["simplywallst", "simply-wall-st", "simply"]

    def test_is_capped(self):
        assert len(slug_variants("One Two Three Four Five")) <= 3

    def test_empty_name_yields_nothing(self):
        assert slug_variants("") == []
        assert slug_variants("   ") == []

    def test_punctuation_only_yields_nothing(self):
        assert slug_variants("!!!") == []


class TestLoadCandidates:
    def test_loads_bare_names(self, write_candidates):
        candidates = load_candidates(write_candidates(["Example Co", "Other Co"]))
        assert [c.name for c in candidates] == ["Example Co", "Other Co"]
        assert candidates[0].has_hint is False

    def test_loads_hints(self, write_candidates):
        path = write_candidates([{"name": "Example Co", "platform": "greenhouse", "identifier": "exampleco"}])
        candidate = load_candidates(path)[0]
        assert candidate.has_hint is True
        assert candidate.platform == "greenhouse"

    def test_drops_unsupported_platform_hint(self, write_candidates):
        # "workday" used to be the example of an unsupported platform here;
        # it has an adapter now, so this uses one that genuinely doesn't.
        path = write_candidates([{"name": "Example Co", "platform": "pageup", "identifier": "x"}])
        candidate = load_candidates(path)[0]
        assert candidate.has_hint is False

    def test_skips_entries_without_a_name(self, write_candidates):
        path = write_candidates([{"platform": "greenhouse"}, {"name": "Good Co"}])
        assert [c.name for c in load_candidates(path)] == ["Good Co"]

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_candidates(tmp_path / "nope.json") == []

    def test_malformed_json_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert load_candidates(path) == []

    def test_non_array_returns_empty(self, write_candidates):
        assert load_candidates(write_candidates({"name": "Example Co"})) == []


class TestVerifyBoard:
    def test_accepts_a_board_with_real_postings(self, adapters):
        board = verify_board("Example Co", "greenhouse", "exampleco", adapters=adapters)
        assert board is not None
        assert board.total_postings == 1

    def test_rejects_an_unknown_identifier(self, adapters):
        assert verify_board("Example Co", "greenhouse", "wrongslug", adapters=adapters) is None

    def test_rejects_an_empty_board(self):
        """A slug that resolves but lists nothing is no evidence of hiring."""
        adapters = {"greenhouse": FakeAdapter({"exampleco": []})}
        assert verify_board("Example Co", "greenhouse", "exampleco", adapters=adapters) is None

    def test_rejects_an_unsupported_platform(self, adapters):
        assert verify_board("Example Co", "workday", "exampleco", adapters=adapters) is None

    def test_adapter_failure_is_not_an_error(self, adapters):
        """A wrong guess is the normal case — it must never raise."""
        assert verify_board("Example Co", "greenhouse", "nope", adapters=adapters) is None


class TestDiscoverCompany:
    def test_finds_a_board_by_probing(self, adapters):
        board = discover_company(Candidate(name="Example Co"), adapters=adapters)
        assert board is not None
        assert board.identifier == "exampleco"

    def test_verified_hint_costs_one_request(self, adapters):
        candidate = Candidate(name="Example Co", platform="greenhouse", identifier="exampleco")
        discover_company(candidate, adapters=adapters)
        assert adapters["greenhouse"].calls == ["exampleco"]

    def test_a_stale_hint_falls_back_to_probing(self, adapters):
        """A hint is verified, never trusted."""
        candidate = Candidate(name="Example Co", platform="greenhouse", identifier="oldslug")
        board = discover_company(candidate, adapters=adapters)
        assert board is not None
        assert board.identifier == "exampleco"
        assert "oldslug" in adapters["greenhouse"].calls

    def test_unknown_company_returns_none(self, adapters):
        assert discover_company(Candidate(name="Nobody At All"), adapters=adapters) is None

    def test_probing_stops_at_the_first_hit(self, adapters):
        """Efficiency: no further slugs are tried once a board resolves."""
        discover_company(Candidate(name="Example Co"), adapters=adapters)
        assert adapters["greenhouse"].calls == ["exampleco"]

    def test_probing_is_bounded(self, adapters):
        discover_company(Candidate(name="Some Long Company Name Here"), adapters=adapters)
        assert len(adapters["greenhouse"].calls) <= 3


class TestRelevance:
    def test_counts_relevant_postings(self, adapters):
        board = discover_company(Candidate(name="Example Co"), adapters=adapters)
        assert board.relevant_count == 1

    def test_irrelevant_postings_are_not_counted(self):
        adapters = {"greenhouse": FakeAdapter({"exampleco": [_posting(title="Senior Tax Accountant")]})}
        board = discover_company(Candidate(name="Example Co"), adapters=adapters)
        assert board.total_postings == 1
        assert board.relevant_count == 0


class TestDeduplicate:
    def test_keeps_the_board_with_more_postings(self):
        boards = [
            VerifiedBoard("Example Co", "lever", "example", [_posting()]),
            VerifiedBoard("Example Co", "greenhouse", "exampleco", [_posting(), _posting()]),
        ]
        deduped = deduplicate(boards)
        assert len(deduped) == 1
        assert deduped[0].platform == "greenhouse"

    def test_different_companies_both_kept(self):
        boards = [
            VerifiedBoard("A Co", "greenhouse", "a", [_posting()]),
            VerifiedBoard("B Co", "greenhouse", "b", [_posting()]),
        ]
        assert len(deduplicate(boards)) == 2

    def test_two_names_resolving_to_one_board_collapse(self):
        """
        Regression: "Deputy" and "Deputy AU" both verified against
        lever/deputy in a real run and were emitted as two separate outreach
        targets. Different names pointing at the same board are one company.
        """
        boards = [
            VerifiedBoard("Deputy", "lever", "deputy", [_posting()]),
            VerifiedBoard("Deputy AU", "lever", "deputy", [_posting()]),
        ]
        deduped = deduplicate(boards)
        assert len(deduped) == 1
        assert deduped[0].company == "Deputy"  # the plainer name wins

    def test_same_identifier_on_different_platforms_is_not_collapsed(self):
        """Two genuinely different companies can share a slug across
        platforms — those must stay separate."""
        boards = [
            VerifiedBoard("A Co", "lever", "shared", [_posting()]),
            VerifiedBoard("B Co", "greenhouse", "shared", [_posting()]),
        ]
        assert len(deduplicate(boards)) == 2

    def test_name_spelling_differences_dedupe(self):
        boards = [
            VerifiedBoard("Example Co", "greenhouse", "a", [_posting()]),
            VerifiedBoard("  EXAMPLE CO  ", "lever", "b", [_posting()]),
        ]
        assert len(deduplicate(boards)) == 1


class TestMergeIntoConfig:
    def test_adds_a_verified_company(self, outreach_config):
        board = VerifiedBoard("Example Co", "greenhouse", "exampleco", [_posting()])
        added, already = merge_into_outreach_config([board], config_path=outreach_config, write=True)

        assert added == ["Example Co"]
        entries = json.loads(outreach_config.read_text())
        assert entries[0]["company"] == "Example Co"
        assert entries[0]["platform"] == "greenhouse"
        assert entries[0]["identifier"] == "exampleco"

    def test_leaves_website_and_email_blank(self, outreach_config):
        """Discovery verifies boards, not addresses — inventing either would
        be exactly the fabrication the project forbids."""
        board = VerifiedBoard("Example Co", "greenhouse", "exampleco", [_posting()])
        merge_into_outreach_config([board], config_path=outreach_config, write=True)
        entry = json.loads(outreach_config.read_text())[0]
        assert entry["website"] == ""
        assert entry["contact_email"] == ""

    def test_does_not_write_without_the_flag(self, outreach_config):
        board = VerifiedBoard("Example Co", "greenhouse", "exampleco", [_posting()])
        merge_into_outreach_config([board], config_path=outreach_config, write=False)
        assert json.loads(outreach_config.read_text()) == []

    def test_preserves_a_manually_edited_entry(self, outreach_config):
        """The critical guarantee: hand-entered data survives a discovery run."""
        outreach_config.write_text(json.dumps([{
            "company": "Example Co", "website": "https://example.invalid",
            "platform": "greenhouse", "identifier": "handpicked",
            "contact_email": "careers@example.invalid", "notes": "mine", "enabled": True,
        }]))
        board = VerifiedBoard("Example Co", "greenhouse", "autodiscovered", [_posting()])

        added, already = merge_into_outreach_config([board], config_path=outreach_config, write=True)

        assert added == []
        assert already == ["Example Co"]
        entry = json.loads(outreach_config.read_text())[0]
        assert entry["contact_email"] == "careers@example.invalid"
        assert entry["identifier"] == "handpicked"
        assert entry["notes"] == "mine"

    def test_appends_alongside_existing_entries(self, outreach_config):
        outreach_config.write_text(json.dumps([{"company": "Existing Co", "enabled": True}]))
        board = VerifiedBoard("New Co", "greenhouse", "newco", [_posting()])
        merge_into_outreach_config([board], config_path=outreach_config, write=True)

        names = [e["company"] for e in json.loads(outreach_config.read_text())]
        assert names == ["Existing Co", "New Co"]

    def test_output_is_loadable_by_the_targets_loader(self, outreach_config):
        from src.outreach.targets import load_targets

        board = VerifiedBoard("Example Co", "greenhouse", "exampleco", [_posting()])
        merge_into_outreach_config([board], config_path=outreach_config, write=True)

        targets = load_targets(outreach_config)
        assert targets[0].company == "Example Co"
        assert targets[0].has_job_board() is True
        assert targets[0].contact_email == ""


class TestCareersPagePath:
    """
    The bottleneck this change removes: a company on none of the four ATS
    platforms is still discoverable, through its own careers page, as long
    as a real website was supplied for it — never guessed.
    """

    def test_verifies_via_structured_job_data(self):
        board = verify_careers_page(
            "Example Co", "https://example.invalid",
            fetch=_fetcher({"https://example.invalid/careers": CAREERS_PAGE_WITH_JOBS}),
        )
        assert board is not None
        assert board.platform == "careers_page"
        assert board.jobs_unavailable is False
        assert board.total_postings == 1
        assert board.postings[0].title == "Service Desk Analyst"

    def test_reachable_page_with_no_structured_data_is_still_verified(self):
        """
        The exact behavior the user asked for: a real, reachable careers
        page that happens to be a Workday widget with no JSON-LD is NOT
        thrown away and NOT reported as "confirmed zero openings".
        """
        board = verify_careers_page(
            "Example Co", "https://example.invalid",
            fetch=_fetcher({"https://example.invalid/careers": CAREERS_PAGE_NO_STRUCTURED_DATA}),
        )
        assert board is not None
        assert board.jobs_unavailable is True
        assert board.total_postings == 0
        assert board.detected_system == "Workday"
        assert board.priority_tier == 4

    def test_nothing_reachable_means_not_verified(self):
        assert verify_careers_page("Example Co", "https://example.invalid", fetch=_fetcher({})) is None

    def test_discover_company_falls_back_to_the_careers_page(self):
        """
        No ATS hint resolves (the FakeAdapter fixture below never matches
        any slug for this name), but a website is supplied — so the company
        is still verified, via the new path.
        """
        candidate = Candidate(name="Zzz Nomatch Co", website="https://example.invalid")
        board = discover_company(
            candidate,
            adapters={"greenhouse": FakeAdapter({})},
            fetch=_fetcher({"https://example.invalid/careers": CAREERS_PAGE_WITH_JOBS}),
        )
        assert board is not None
        assert board.platform == "careers_page"

    def test_a_verified_but_unavailable_company_is_still_written_to_the_registry(
        self, write_candidates, outreach_config
    ):
        candidates = write_candidates([{"name": "Example Co", "website": "https://example.invalid"}])
        result = run_discovery(
            candidates_path=candidates, config_path=outreach_config,
            adapters={"greenhouse": FakeAdapter({})},
            fetch=_fetcher({"https://example.invalid/careers": CAREERS_PAGE_NO_STRUCTURED_DATA}),
            write=True,
        )
        assert result.added == ["Example Co"]
        entry = json.loads(outreach_config.read_text())[0]
        assert entry["website"] == "https://example.invalid"
        assert entry["platform"] == "careers_page"
        assert "not machine-readable" in entry["notes"]

    def test_load_candidates_reads_the_website_hint(self, tmp_path):
        path = tmp_path / "candidates.json"
        path.write_text(json.dumps([{"name": "Example Co", "website": "https://example.invalid"}]))
        candidates = load_candidates(path)
        assert candidates[0].website == "https://example.invalid"

    def test_a_verified_hint_is_preferred_over_the_careers_page(self):
        """The cheapest, most authoritative source wins when both would
        resolve: an explicit platform/identifier hint."""
        candidate = Candidate(
            name="Example Co", website="https://example.invalid",
            platform="greenhouse", identifier="exampleco",
        )
        board = discover_company(
            candidate,
            adapters={"greenhouse": FakeAdapter({"exampleco": [_posting()]})},
            fetch=_fetcher({"https://example.invalid/careers": CAREERS_PAGE_WITH_JOBS}),
        )
        assert board is not None
        assert board.platform == "greenhouse"

    def test_a_website_skips_ats_slug_guessing_entirely(self):
        """
        The scale optimisation: guessing ATS slugs for a company that already
        carries a verified website would hammer the four shared ATS hosts for
        nothing, across every non-startup employer in the registry (banks,
        universities, government...). A website with no hint goes straight to
        the careers-page probe — the ATS adapter is never even consulted.
        """
        candidate = Candidate(name="Example Co", website="https://example.invalid")

        class ExplodingAdapter(FakeAdapter):
            def discover(self, identifier):
                raise AssertionError("ATS slug-guessing must not run when a website is present")

        board = discover_company(
            candidate,
            adapters={
                "greenhouse": ExplodingAdapter({}), "lever": ExplodingAdapter({}),
                "ashby": ExplodingAdapter({}), "smartrecruiters": ExplodingAdapter({}),
            },
            fetch=_fetcher({"https://example.invalid/careers": CAREERS_PAGE_WITH_JOBS}),
        )
        assert board is not None
        assert board.platform == "careers_page"

    def test_ats_slug_guessing_still_runs_when_there_is_no_website(self):
        """The only remaining avenue for a bare-name candidate — unchanged."""
        candidate = Candidate(name="Example Co")
        board = discover_company(
            candidate,
            adapters={"greenhouse": FakeAdapter({"exampleco": [_posting()]})},
            fetch=_fetcher({}),
        )
        assert board is not None
        assert board.platform == "greenhouse"


class TestPriorityTier:
    def test_tier_1_is_a_clearing_entry_level_opening(self):
        board = VerifiedBoard("Co", "greenhouse", "co", [_posting(title="Service Desk Analyst")])
        assert board.priority_tier == 1

    def test_tier_2_is_graduate_or_junior_tech(self):
        board = VerifiedBoard(
            "Co", "careers_page", "https://example.invalid/careers",
            [_posting(title="Graduate Software Engineer", description="Work with our Python backend.")],
        )
        assert board.priority_tier == 2

    def test_tier_3_is_any_other_tech_opening(self):
        board = VerifiedBoard(
            "Co", "careers_page", "https://example.invalid/careers",
            [_posting(title="Senior Backend Engineer", description="Deep Kubernetes and AWS experience required.")],
        )
        assert board.priority_tier == 3

    def test_tier_4_is_nothing_tech_relevant_or_unavailable(self):
        board = VerifiedBoard(
            "Co", "greenhouse", "co",
            [_posting(title="Warehouse Assistant", description="Pack boxes and load trucks.")],
        )
        assert board.priority_tier == 4

    def test_jobs_unavailable_is_always_tier_4(self):
        board = VerifiedBoard("Co", "careers_page", "https://example.invalid", [], jobs_unavailable=True)
        assert board.priority_tier == 4


class TestAlreadyKnownCandidatesAreSkipped:
    """
    The other half of the scale optimisation: a candidate already present in
    the outreach registry costs nothing on a repeat run — not even the
    network call a fresh verification would need.
    """

    def test_no_network_call_for_an_already_known_company(self, write_candidates, outreach_config):
        outreach_config.write_text(json.dumps([{"company": "Example Co", "enabled": True}]))

        class ExplodingAdapter(FakeAdapter):
            def discover(self, identifier):
                raise AssertionError("an already-known candidate must never be re-probed")

        result = run_discovery(
            candidates_path=write_candidates(["Example Co"]),
            config_path=outreach_config,
            adapters={"greenhouse": ExplodingAdapter({})},
            fetch=_fetcher({}),
        )
        assert result.already_present == ["Example Co"]
        assert result.verified == []

    def test_an_unknown_candidate_alongside_a_known_one_is_still_processed(
        self, write_candidates, outreach_config
    ):
        outreach_config.write_text(json.dumps([{"company": "Old Co", "enabled": True}]))
        result = run_discovery(
            candidates_path=write_candidates(["Old Co", "New Co"]),
            config_path=outreach_config,
            adapters={"greenhouse": FakeAdapter({"newco": [_posting()]})},
            fetch=_fetcher({}),
            write=True,
        )
        assert result.already_present == ["Old Co"]
        assert result.added == ["New Co"]


class TestRunDiscovery:
    def test_end_to_end(self, write_candidates, outreach_config, adapters):
        candidates = write_candidates(["Example Co", "Nobody At All"])
        result = run_discovery(
            candidates_path=candidates, config_path=outreach_config,
            adapters=adapters, write=True,
        )

        assert result.researched == 2
        assert len(result.verified) == 1
        assert len(result.with_relevant_openings) == 1
        assert result.added == ["Example Co"]
        assert result.unverified == ["Nobody At All"]

    def test_keeps_companies_with_no_relevant_openings_by_default(self, write_candidates, outreach_config):
        """
        Standing rule: a legitimate, verified company is never discarded
        just because nothing it currently advertises matches — it lands in
        the lowest priority tier instead, and stays in the registry for a
        later run to notice if that changes.
        """
        adapters = {
            "greenhouse": FakeAdapter({
                "exampleco": [_posting(title="Senior Tax Accountant", description="Prepare client tax returns.")]
            })
        }
        result = run_discovery(
            candidates_path=write_candidates(["Example Co"]), config_path=outreach_config,
            adapters=adapters, write=True,
        )
        assert len(result.verified) == 1
        assert result.with_relevant_openings == []
        assert result.verified[0].priority_tier == 4
        assert result.added == ["Example Co"]

    def test_relevant_only_narrows_to_tier_one(self, write_candidates, outreach_config):
        adapters = {"greenhouse": FakeAdapter({"exampleco": [_posting(title="Senior Tax Accountant")]})}
        result = run_discovery(
            candidates_path=write_candidates(["Example Co"]), config_path=outreach_config,
            adapters=adapters, write=True, min_tier=1,
        )
        assert result.added == []

    def test_limit_caps_how_many_are_checked(self, write_candidates, outreach_config, adapters):
        result = run_discovery(
            candidates_path=write_candidates(["Example Co", "Other Co", "Third Co"]),
            config_path=outreach_config, adapters=adapters, limit=1,
        )
        assert result.researched == 1

    def test_rerunning_adds_nothing_new(self, write_candidates, outreach_config, adapters):
        candidates = write_candidates(["Example Co"])
        run_discovery(candidates_path=candidates, config_path=outreach_config,
                      adapters=adapters, write=True)
        second = run_discovery(candidates_path=candidates, config_path=outreach_config,
                               adapters=adapters, write=True)
        assert second.added == []
        assert second.already_present == ["Example Co"]
        assert len(json.loads(outreach_config.read_text())) == 1

    def test_result_is_serializable(self, write_candidates, outreach_config, adapters):
        result = run_discovery(candidates_path=write_candidates(["Example Co"]),
                               config_path=outreach_config, adapters=adapters)
        assert json.loads(json.dumps(result.to_dict()))["verified"] == 1

    def test_nothing_invented_when_no_board_resolves(self, write_candidates, outreach_config, adapters):
        """A candidate name that verifies nowhere must produce no entry."""
        result = run_discovery(
            candidates_path=write_candidates(["Totally Made Up Pty Ltd"]),
            config_path=outreach_config, adapters=adapters, write=True,
        )
        assert result.verified == []
        assert json.loads(outreach_config.read_text()) == []


class TestScopeIsUnchanged:
    def test_discovery_does_not_touch_the_pipeline_or_sender(self):
        import pathlib

        source = pathlib.Path("src/outreach/discovery.py").read_text()
        for token in ("smtplib", "send_approved", "approve_message", "gate_and_record",
                      "draft_outreach_email", "get_ai_provider"):
            assert token not in source, f"discovery.py references {token!r}"

    def test_discovery_adds_no_new_http_client(self):
        """All requests go through the existing adapters, so the existing
        rate limiting and User-Agent apply."""
        import pathlib

        source = pathlib.Path("src/outreach/discovery.py").read_text()
        assert "import requests" not in source
        # No direct call to the low-level fetcher either — only mentions of
        # it in prose. Adapters are the sole request path.
        assert "polite_get(" not in source


CAREERS_PAGE_LINKING_TO_WORKDAY = """
<html><body>
<h1>Careers at Example Co</h1>
<a href="https://example.wd3.myworkdayjobs.com/en-US/Example_Careers">Search our jobs</a>
</body></html>
"""


class TestRefreshExistingEntries:
    """
    The one pass that MODIFIES entries already on file. Its whole reason to
    exist is upgrading the careers_page backlog to real ATS boards, and its
    whole risk is trampling something the user typed by hand — so both are
    tested here.
    """

    @pytest.fixture()
    def registry(self, tmp_path):
        path = tmp_path / "outreach_companies.json"
        path.write_text(json.dumps([
            {
                "company": "Example Co",
                "website": "https://example.invalid",
                "platform": "careers_page",
                "identifier": "https://example.invalid/careers",
                "contact_email": "careers@example.invalid",
                "notes": "Discovered 2026-01-01: not machine-readable.",
                "enabled": False,
            },
            {
                "company": "Already On Greenhouse",
                "website": "https://gh.invalid",
                "platform": "greenhouse",
                "identifier": "exampleco",
                "contact_email": "",
                "notes": "",
                "enabled": True,
            },
        ]))
        return path

    def _fetch(self):
        return _fetcher({"https://example.invalid/careers": CAREERS_PAGE_LINKING_TO_WORKDAY})

    def test_a_careers_page_entry_is_upgraded_to_its_real_ats(self, registry):
        result = refresh_careers_page_entries(
            config_path=registry,
            adapters={"workday": FakeAdapter({
                "https://example.wd3.myworkdayjobs.com/example/Example_Careers": [_posting()],
            })},
            fetch=self._fetch(),
            write=True,
        )
        assert result.processed == 1  # the Greenhouse entry was never touched
        assert result.upgraded == ["Example Co: careers_page -> workday"]

        entry = json.loads(registry.read_text())[0]
        assert entry["platform"] == "workday"
        assert entry["identifier"] == "https://example.wd3.myworkdayjobs.com/example/Example_Careers"

    def test_contact_email_and_enabled_are_never_overwritten(self, registry):
        """The hard requirement: an address found by hand, and a company
        deliberately switched off, must both survive an upgrade."""
        refresh_careers_page_entries(
            config_path=registry,
            adapters={"workday": FakeAdapter({
                "https://example.wd3.myworkdayjobs.com/example/Example_Careers": [_posting()],
            })},
            fetch=self._fetch(),
            write=True,
        )
        entry = json.loads(registry.read_text())[0]
        assert entry["contact_email"] == "careers@example.invalid"
        assert entry["enabled"] is False

    def test_entries_on_a_real_ats_are_left_completely_alone(self, registry):
        refresh_careers_page_entries(
            config_path=registry, adapters={}, fetch=self._fetch(), write=True,
        )
        entry = json.loads(registry.read_text())[1]
        assert entry == {
            "company": "Already On Greenhouse", "website": "https://gh.invalid",
            "platform": "greenhouse", "identifier": "exampleco",
            "contact_email": "", "notes": "", "enabled": True,
        }

    def test_nothing_is_written_without_write(self, registry):
        before = registry.read_text()
        result = refresh_careers_page_entries(
            config_path=registry,
            adapters={"workday": FakeAdapter({
                "https://example.wd3.myworkdayjobs.com/example/Example_Careers": [_posting()],
            })},
            fetch=self._fetch(),
        )
        assert result.upgraded  # it found the upgrade
        assert registry.read_text() == before  # and changed nothing on disk

    def test_an_unreachable_site_leaves_the_entry_untouched(self, registry):
        """A site being down today is not evidence the company isn't real —
        the entry is reported, never downgraded or dropped."""
        before = json.loads(registry.read_text())
        result = refresh_careers_page_entries(
            config_path=registry, adapters={}, fetch=_fetcher({}), write=True,
        )
        assert result.unreachable == ["Example Co"]
        assert json.loads(registry.read_text()) == before

    def test_an_ats_with_no_adapter_is_recorded_not_guessed(self, registry):
        """PageUp is recognised and counted for the report, but the entry
        stays careers_page — there is no verified public endpoint to query,
        so nothing pretends otherwise."""
        page = (
            '<html><body><h1>Careers</h1>'
            '<a href="https://example.pageuppeople.com/search/en">Current vacancies</a>'
            "</body></html>"
        )
        result = refresh_careers_page_entries(
            config_path=registry,
            adapters={},
            fetch=_fetcher({"https://example.invalid/careers": page}),
            write=True,
        )
        assert result.detected_only == {"PageUp": 1}
        assert json.loads(registry.read_text())[0]["platform"] == "careers_page"

    def test_the_notes_record_how_the_board_was_found(self, registry):
        refresh_careers_page_entries(
            config_path=registry,
            adapters={"workday": FakeAdapter({
                "https://example.wd3.myworkdayjobs.com/example/Example_Careers": [_posting()],
            })},
            fetch=self._fetch(),
            write=True,
        )
        notes = json.loads(registry.read_text())[0]["notes"]
        assert "workday" in notes
        assert "example.wd3.myworkdayjobs.com" in notes
