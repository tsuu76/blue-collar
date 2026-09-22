"""
Tests for the outreach orchestrator.

No network and no Ollama: a fake ATS adapter supplies postings and
FakeAIProvider supplies both AI calls. Nothing here can send email — the
outreach package imports no mail library at all, which one test asserts
directly.

Every company, posting and email body below is a synthetic fixture.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from src.ai.base import AIResponseError
from src.database.db import get_connection, init_db
from src.database.models import OutreachStatus
from src.database.outreach_repo import (
    approve_message,
    get_company,
    insert_company,
    insert_message,
    list_messages_for_company,
    mark_message_sent,
    set_do_not_contact,
)
from src.job_discovery.base import JobDiscoverySource
from src.job_discovery.registry import EmployerConfig
from src.outreach import pipeline as pipeline_module
from src.outreach.pipeline import Outcome, run_outreach
from src.resume.schema import Bullet, Education, MasterResume, Personal, Project
from src.sources.base import NormalizedJob
from tests.fakes import FakeAIProvider

POSTING_DESCRIPTION = (
    "Troubleshoot customer issues on our service desk. Python and SQL are used daily "
    "by the team. Entry level applicants welcome."
)


class FakeAdapter(JobDiscoverySource):
    """Stands in for a real ATS adapter — returns scripted postings or raises."""

    platform = "greenhouse"

    def __init__(self, postings=None, error: Exception | None = None):
        self._postings = postings if postings is not None else [_posting()]
        self._error = error
        self.calls: list[str] = []

    def discover(self, identifier: str) -> list[NormalizedJob]:
        self.calls.append(identifier)
        if self._error:
            raise self._error
        return list(self._postings)


def _posting(**overrides) -> NormalizedJob:
    return NormalizedJob(
        **{
            "source": "greenhouse",
            "url": "https://example.invalid/jobs/1",
            "title": "Service Desk Analyst",
            "description": POSTING_DESCRIPTION,
            "company": "Example Co",
            "location": "Sydney, NSW",
            "source_job_id": "1",
            **overrides,
        }
    )


ANALYSIS_JSON = {
    "recurring_skills": ["Python", "SQL"],
    "tools_and_technologies": ["Python"],
    "responsibilities": ["Troubleshoot customer issues"],
    "experience_requirements": ["Entry level"],
    "terminology": ["Service Desk"],
    "candidate_overlap": ["Python", "SQL"],
    "notes": "They hire support-focused technical staff.",
}

GOOD_BODY = " ".join(
    [
        "Hi there,",
        "I had a look at what Example Co is hiring for and the Service Desk Analyst role",
        "stood out, because so much of it is troubleshooting with Python and SQL in the mix.",
        "Those are the two things I have spent the most time on so far.",
        "I am a first-year IT student. Most of my practice has come from building TestApp,",
        "a small expense tracker I wrote in Python with a SQL database behind it, plus the",
        "networking side of my course. I am not going to pretend that adds up to industry",
        "experience, but I can troubleshoot patiently and I pick things up quickly.",
        "I am looking for a first proper opportunity, so anything entry level, junior or",
        "support based would be great. If there is someone better placed for me to talk to,",
        "I would appreciate a pointer in their direction.",
        "Thanks for your time,",
        "Test",
    ]
)
GOOD_EMAIL = f"Subject: Student interested in entry-level work\n\n{GOOD_BODY}"


@pytest.fixture()
def resume() -> MasterResume:
    return MasterResume(
        personal=Personal(full_name="Test Candidate", email="test@example.invalid"),
        summary="First-year IT student studying cybersecurity.",
        skills=["Python", "SQL", "Git"],
        education=[
            Education(
                id="edu_01",
                institution="Example University",
                credential="Bachelor of IT",
                highlights=[Bullet(id="edu_01_b1", text="Studied networking fundamentals.")],
            )
        ],
        projects=[
            Project(
                id="proj_01",
                name="TestApp",
                technologies=["Python", "SQL"],
                bullets=[
                    Bullet(id="proj_01_b1", text="Built a small expense tracker in Python.", skills=["Python"])
                ],
            )
        ],
    )


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "outreach.db"
    init_db(path)
    return path


@pytest.fixture()
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture()
def config(tmp_path):
    """One enabled, fully-configured target."""
    def _write(entries=None):
        path = tmp_path / "outreach_companies.json"
        path.write_text(
            json.dumps(
                entries
                if entries is not None
                else [
                    {
                        "company": "Example Co",
                        "website": "https://example.invalid",
                        "platform": "greenhouse",
                        "identifier": "exampleco",
                        "contact_email": "careers@example.invalid",
                    }
                ]
            )
        )
        return path

    return _write


@pytest.fixture()
def adapter():
    return FakeAdapter()


@pytest.fixture()
def run(db_path, config, resume, adapter):
    """Run the pipeline with everything faked and nothing on the network."""
    def _run(*, entries=None, json_responses=None, text_responses=None, **kwargs):
        return run_outreach(
            db_path=db_path,
            config_path=config(entries),
            resume=resume,
            analysis_provider=FakeAIProvider(
                json_responses=json_responses if json_responses is not None else [ANALYSIS_JSON] * 5
            ),
            email_provider=FakeAIProvider(
                text_responses=text_responses if text_responses is not None else [GOOD_EMAIL] * 5
            ),
            adapters={"greenhouse": adapter},
            employers=[],
            **kwargs,
        )

    return _run


class TestHappyPath:
    def test_drafts_an_email(self, run, conn):
        result = run()
        assert result.drafted == 1
        assert result.failed == 0
        assert result.outcomes[0].outcome == Outcome.DRAFTED

    def test_stores_it_as_draft(self, run, conn):
        result = run()
        message_id = result.outcomes[0].message_id
        from src.database.outreach_repo import get_message

        row = get_message(conn, message_id)
        assert row["status"] == OutreachStatus.DRAFT
        assert row["sent_at"] is None
        assert row["send_attempts"] == 0
        assert row["recipient_email"] == "careers@example.invalid"

    def test_creates_the_company_from_config_only(self, run, conn):
        run()
        from src.database.outreach_repo import list_companies

        companies = list_companies(conn)
        assert [c["name"] for c in companies] == ["Example Co"]

    def test_stores_research_evidence(self, run, conn):
        result = run()
        company = get_company(conn, result.outcomes[0].company_id)
        research = json.loads(company["research_json"])
        assert research["posting_count"] == 1
        assert research["titles"] == ["Service Desk Analyst"]
        assert company["researched_at"] is not None

    def test_uses_the_real_adapter_identifier(self, run, adapter):
        run()
        assert adapter.calls == ["exampleco"]

    def test_no_targets_configured_does_nothing(self, run, adapter):
        result = run(entries=[])
        assert result.targets_considered == 0
        assert result.drafted == 0
        assert adapter.calls == []


class TestIdempotency:
    def test_second_run_creates_no_duplicate_draft(self, run, conn):
        """The core idempotency guarantee."""
        first = run()
        assert first.drafted == 1

        second = run()
        assert second.drafted == 0
        assert second.outcomes[0].outcome == Outcome.SKIPPED_PENDING_MESSAGE

        company_id = first.outcomes[0].company_id
        assert len(list_messages_for_company(conn, company_id)) == 1

    def test_second_run_does_not_duplicate_the_company(self, run, conn):
        run()
        run()
        from src.database.outreach_repo import list_companies

        assert len(list_companies(conn)) == 1

    def test_approved_draft_also_blocks_a_redraft(self, run, conn):
        result = run()
        approve_message(conn, result.outcomes[0].message_id)
        conn.commit()

        again = run()
        assert again.outcomes[0].outcome == Outcome.SKIPPED_PENDING_MESSAGE

    def test_already_contacted_company_is_skipped(self, run, conn):
        result = run()
        message_id = result.outcomes[0].message_id
        approve_message(conn, message_id)
        mark_message_sent(conn, message_id)
        conn.commit()

        again = run()
        assert again.outcomes[0].outcome == Outcome.SKIPPED_ALREADY_CONTACTED

    def test_skip_happens_before_any_network_call(self, run, conn, adapter):
        run()
        adapter.calls.clear()
        run()
        assert adapter.calls == []


class TestSkipConditions:
    def test_do_not_contact(self, run, conn, adapter):
        company_id = insert_company(
            conn, {"name": "Example Co", "website": "https://example.invalid"}
        )
        set_do_not_contact(conn, company_id, "asked not to be contacted")
        conn.commit()

        result = run()
        assert result.outcomes[0].outcome == Outcome.SKIPPED_DO_NOT_CONTACT
        assert "asked not to be contacted" in result.outcomes[0].reason
        assert adapter.calls == []  # no network call for a company we can't email

    def test_no_postings(self, db_path, config, resume):
        result = run_outreach(
            db_path=db_path,
            config_path=config(),
            resume=resume,
            analysis_provider=FakeAIProvider(json_responses=[ANALYSIS_JSON]),
            email_provider=FakeAIProvider(text_responses=[GOOD_EMAIL]),
            adapters={"greenhouse": FakeAdapter(postings=[])},
            employers=[],
        )
        assert result.outcomes[0].outcome == Outcome.SKIPPED_NO_POSTINGS
        assert result.drafted == 0

    def test_board_failure_is_a_skip_not_a_crash(self, db_path, config, resume):
        result = run_outreach(
            db_path=db_path,
            config_path=config(),
            resume=resume,
            analysis_provider=FakeAIProvider(json_responses=[ANALYSIS_JSON]),
            email_provider=FakeAIProvider(text_responses=[GOOD_EMAIL]),
            adapters={"greenhouse": FakeAdapter(error=RuntimeError("404 Not Found"))},
            employers=[],
        )
        assert result.outcomes[0].outcome == Outcome.SKIPPED_NO_POSTINGS
        assert "404" in result.outcomes[0].reason

    def test_no_contact_email(self, run):
        result = run(entries=[
            {
                "company": "Example Co",
                "website": "https://example.invalid",
                "platform": "greenhouse",
                "identifier": "exampleco",
            }
        ])
        assert result.outcomes[0].outcome == Outcome.SKIPPED_NO_CONTACT_EMAIL

    def test_no_contact_email_still_records_research(self, run, conn):
        """Research is kept so the dashboard can show what they're hiring
        for — that's what tells you whether finding an address is worth it."""
        result = run(entries=[
            {
                "company": "Example Co",
                "website": "https://example.invalid",
                "platform": "greenhouse",
                "identifier": "exampleco",
            }
        ])
        company = get_company(conn, result.outcomes[0].company_id)
        assert json.loads(company["research_json"])["posting_count"] == 1

    def test_no_verified_overlap(self, run):
        """A model claiming only skills the candidate lacks leaves no honest
        email to write."""
        result = run(json_responses=[{**ANALYSIS_JSON, "candidate_overlap": ["AWS", "Java"]}])
        assert result.outcomes[0].outcome == Outcome.SKIPPED_NO_OVERLAP
        assert result.drafted == 0

    def test_no_ats_board_configured(self, run):
        result = run(entries=[
            {
                "company": "Example Co",
                "website": "https://example.invalid",
                "contact_email": "careers@example.invalid",
            }
        ])
        assert result.outcomes[0].outcome == Outcome.SKIPPED_NO_POSTINGS

    def test_disabled_target_is_never_considered(self, run, adapter):
        result = run(entries=[
            {
                "company": "Example Co",
                "platform": "greenhouse",
                "identifier": "exampleco",
                "contact_email": "careers@example.invalid",
                "enabled": False,
            }
        ])
        assert result.targets_considered == 0
        assert adapter.calls == []


class TestFailures:
    def test_analysis_failure_is_recorded_not_raised(self, run):
        result = run(json_responses=[AIResponseError("model unavailable")] * 5)
        assert result.outcomes[0].outcome == Outcome.FAILED_ANALYSIS
        assert result.failed == 1
        assert result.drafted == 0

    def test_rejected_draft_is_not_stored(self, run, conn):
        """An email that fails verification is rejected, never stored with
        invented content patched in."""
        fabricating = f"Subject: Hi there\n\n{GOOD_BODY} I am also certified in Kubernetes."
        result = run(text_responses=[fabricating] * 6)
        assert result.outcomes[0].outcome == Outcome.FAILED_DRAFT
        assert list_messages_for_company(conn, result.outcomes[0].company_id) == []

    def test_one_failure_does_not_stop_the_batch(self, db_path, tmp_path, resume, adapter):
        path = tmp_path / "two.json"
        path.write_text(json.dumps([
            {"company": "Broken Co", "platform": "greenhouse", "identifier": "broken",
             "contact_email": "careers@broken.invalid"},
            {"company": "Good Co", "platform": "greenhouse", "identifier": "good",
             "contact_email": "careers@good.invalid"},
        ]))
        result = run_outreach(
            db_path=db_path,
            config_path=path,
            resume=resume,
            analysis_provider=FakeAIProvider(
                json_responses=[AIResponseError("boom"), AIResponseError("boom"),
                                AIResponseError("boom"), AIResponseError("boom"), ANALYSIS_JSON]
            ),
            email_provider=FakeAIProvider(text_responses=[GOOD_EMAIL]),
            adapters={"greenhouse": adapter},
            employers=[],
        )
        assert result.failed == 1
        assert result.drafted == 1


class TestDailyLimit:
    def _entries(self, n):
        # Names deliberately contain "Example Co" so the shared GOOD_EMAIL
        # body stays truthful for each of them — naming a company the
        # postings never mention is exactly what the fabrication guard
        # rejects, and it would reject it here too.
        return [
            {
                "company": f"Example Co {i}",
                "website": f"https://example{i}.invalid",
                "platform": "greenhouse",
                "identifier": f"example{i}",
                "contact_email": f"careers@example{i}.invalid",
            }
            for i in range(n)
        ]

    def test_limit_caps_drafts_created(self, run, conn):
        result = run(entries=self._entries(4), limit=2)
        assert result.drafted == 2
        assert sum(1 for o in result.outcomes if o.outcome == Outcome.SKIPPED_DAILY_LIMIT) == 2

    def test_configured_daily_limit_applies(self, db_path, tmp_path, resume, adapter, monkeypatch):
        # Settings is a frozen dataclass — patch the module-level name, the
        # same technique tests/conftest.py uses.
        small = dataclasses.replace(pipeline_module.settings, outreach_daily_limit=1)
        monkeypatch.setattr(pipeline_module, "settings", small)

        path = tmp_path / "many.json"
        path.write_text(json.dumps(self._entries(3)))
        result = run_outreach(
            db_path=db_path,
            config_path=path,
            resume=resume,
            analysis_provider=FakeAIProvider(json_responses=[ANALYSIS_JSON] * 5),
            email_provider=FakeAIProvider(text_responses=[GOOD_EMAIL] * 5),
            adapters={"greenhouse": adapter},
            employers=[],
        )
        assert result.drafted == 1
        assert result.daily_limit == 1

    def test_drafts_already_made_today_count_against_the_limit(self, run, conn):
        """Two runs in one day produce at most the daily limit between them,
        not that many each."""
        company_id = insert_company(conn, {"name": "Pre-existing", "website": "https://pre.invalid"})
        for _ in range(2):
            insert_message(conn, {"company_id": company_id, "subject": "x", "body": "y"})
        conn.commit()

        result = run(entries=self._entries(3), limit=None)
        # 3 targets, but only (daily_limit - 2) of the day's budget remains.
        assert result.drafted <= max(0, result.daily_limit - 2)

    def test_limit_of_zero_drafts_nothing(self, run, adapter):
        result = run(entries=self._entries(2), limit=0)
        assert result.drafted == 0
        assert adapter.calls == []


class TestDryRun:
    def test_writes_nothing(self, run, conn):
        result = run(dry_run=True)
        assert result.dry_run is True
        assert result.drafted == 1  # it really did produce a verified draft
        from src.database.outreach_repo import list_companies

        # ...and then rolled the whole thing back.
        assert list_companies(conn) == []

    def test_still_runs_the_real_pipeline(self, run, adapter):
        run(dry_run=True)
        assert adapter.calls == ["exampleco"]

    def test_leaves_existing_data_untouched(self, run, conn):
        company_id = insert_company(conn, {"name": "Untouched Co", "website": "https://untouched.invalid"})
        conn.commit()

        run(dry_run=True)
        assert get_company(conn, company_id)["name"] == "Untouched Co"

    def test_dry_run_then_real_run_creates_one_draft(self, run, conn):
        run(dry_run=True)
        result = run()
        assert result.drafted == 1
        assert len(list_messages_for_company(conn, result.outcomes[0].company_id)) == 1


class TestSendingStaysConfined:
    """
    The pipeline now sends, but only ever by calling sender.py. These are the
    structural invariants that keep automatic sending honest.
    """

    def test_only_the_sender_module_can_transmit(self):
        """
        SMTP is confined to exactly one module. The pipeline delegates to it
        and cannot open a socket of its own, so there is a single place where
        delivery can be reasoned about.
        """
        import pathlib

        banned = ("smtplib", "import email", "from email", "sendmail", "SMTP(")
        for path in pathlib.Path("src/outreach").glob("*.py"):
            if path.name == "sender.py":
                continue
            source = path.read_text()
            for token in banned:
                assert token not in source, f"{path} references {token!r}"

    def test_the_environment_veto_is_checked_before_anything_else(self):
        """
        sending_status consults OUTREACH_DRY_RUN first, before the run's own
        flags and before SMTP configuration — so no combination of arguments
        can reach a send while the veto is on.
        """
        source = __import__("pathlib").Path("src/outreach/pipeline.py").read_text()
        body = source.split("def sending_status")[1]
        assert body.index("settings.outreach_dry_run") < body.index("if dry_run")

    def test_nothing_is_sent_while_the_veto_is_on(self, run, conn):
        """
        The default posture. OUTREACH_DRY_RUN is true in the test
        environment, so a full run produces a gated draft and stops.
        """
        result = run()
        assert result.sent == 0
        assert result.sending_enabled is False
        assert "OUTREACH_DRY_RUN" in result.sending_blocked_reason
        for message in list_messages_for_company(conn, result.outcomes[0].company_id):
            assert message["status"] == OutreachStatus.DRAFT
            assert message["sent_at"] is None
            assert message["send_attempts"] == 0

    def test_an_injected_transport_cannot_override_the_veto(self, run, conn):
        """
        A transport passed in by a caller (or a test) is not a way around the
        environment switch — it is refused along with everything else.
        """
        from src.outreach.sender import EmailTransport

        class ExplodingTransport(EmailTransport):
            def send(self, message):
                raise AssertionError("the veto leaked — this must never be reached")

        result = run(transport=ExplodingTransport())
        assert result.sent == 0
        assert result.sending_enabled is False

    def test_result_reports_zero_sent(self, run):
        assert run().to_dict()["sent"] == 0


class TestResultShape:
    def test_to_dict_is_serializable(self, run):
        payload = json.loads(json.dumps(run().to_dict()))
        assert payload["drafted"] == 1
        assert payload["sent"] == 0
        assert payload["outcomes"][0]["company"] == "Example Co"

    def test_counts_add_up(self, run):
        result = run(entries=[
            {"company": "A Co", "platform": "greenhouse", "identifier": "a", "contact_email": "a@a.invalid"},
            {"company": "B Co"},
        ])
        assert (
            result.sent + result.drafted + result.skipped + result.failed
            == len(result.outcomes)
        )
