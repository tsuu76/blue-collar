"""
Tests for the "Add to outreach" affordance on the Sources page.

Two layers get exercised here: `src.outreach.targets.add_target` (the
storage-layer append + idempotency), and the POST /sources/<slug>/add-to-outreach
Flask route that wraps it. Both must be idempotent on the underlying
(platform, identifier) — the display name is UI text and can drift, so
using it as the identity would let a double-click still duplicate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dashboard.app import create_app
from src.database.db import init_db
from src.outreach.targets import add_target


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture()
def outreach_path(tmp_path):
    return tmp_path / "outreach_companies.json"


class TestAddTargetHelper:
    def test_creates_file_when_missing(self, outreach_path):
        added, reason = add_target(
            {
                "company": "Example",
                "platform": "greenhouse",
                "identifier": "example",
                "enabled": True,
            },
            config_path=outreach_path,
        )
        assert added is True
        assert reason == "added"
        rows = json.loads(outreach_path.read_text())
        assert len(rows) == 1
        assert rows[0]["company"] == "Example"

    def test_appends_to_existing_file(self, outreach_path):
        outreach_path.write_text(json.dumps([
            {"company": "First", "platform": "greenhouse", "identifier": "first"},
        ]))
        added, reason = add_target(
            {"company": "Second", "platform": "lever", "identifier": "second"},
            config_path=outreach_path,
        )
        assert added is True
        rows = json.loads(outreach_path.read_text())
        assert [r["company"] for r in rows] == ["First", "Second"]

    def test_double_add_reports_already_present_not_duplicate(self, outreach_path):
        entry = {
            "company": "Example",
            "platform": "greenhouse",
            "identifier": "example",
        }
        first_added, _ = add_target(entry, config_path=outreach_path)
        second_added, reason = add_target(entry, config_path=outreach_path)
        assert first_added is True
        assert second_added is False
        assert reason == "already_present"
        rows = json.loads(outreach_path.read_text())
        assert len(rows) == 1

    def test_idempotent_on_platform_identifier_not_display_name(self, outreach_path):
        """
        Ishmam's explicit guidance: the guard is on slug/identifier, not
        display name. "SafetyCulture" and "Safety Culture" must be
        treated as the SAME target when they share (platform, identifier).
        """
        add_target(
            {"company": "SafetyCulture", "platform": "ashby", "identifier": "safetyculture"},
            config_path=outreach_path,
        )
        added, reason = add_target(
            {"company": "Safety Culture", "platform": "ashby", "identifier": "safetyculture"},
            config_path=outreach_path,
        )
        assert added is False
        assert reason == "already_present"

    def test_different_platform_identifier_is_a_different_target(self, outreach_path):
        add_target(
            {"company": "Zip", "platform": "greenhouse", "identifier": "zipcolimited"},
            config_path=outreach_path,
        )
        added, _ = add_target(
            {"company": "Zip", "platform": "ashby", "identifier": "zip"},
            config_path=outreach_path,
        )
        assert added is True
        rows = json.loads(outreach_path.read_text())
        assert len(rows) == 2

    def test_falls_back_to_name_when_no_platform(self, outreach_path):
        # When neither entry has a platform, we deduplicate on
        # normalized name — otherwise every "custom" target with no
        # board could quietly stack.
        add_target({"company": "Bespoke Co"}, config_path=outreach_path)
        added, reason = add_target(
            {"company": "  bespoke co  "}, config_path=outreach_path,
        )
        assert added is False
        assert reason == "already_present"

    def test_empty_company_is_rejected(self, outreach_path):
        added, reason = add_target({"company": "  "}, config_path=outreach_path)
        assert added is False
        assert reason == "no_company_name"
        assert not outreach_path.exists()


class TestAddToOutreachRoute:
    """
    The Flask route. Uses the real config/employers.json to look up
    the SourceRow; tests monkeypatch the OUTREACH_CONFIG_PATH so no
    test writes into the checked-in outreach_companies.json.
    """

    def _pick_employer(self):
        """Any real enabled employer entry from config/employers.json — we
        just need one whose slug we can hit the route with."""
        from src.job_discovery.registry import load_employers
        from src.dashboard.sources_view import slugify_company

        employers = [e for e in load_employers() if e.enabled]
        if not employers:
            pytest.skip("No employers configured")
        target = employers[0]
        return target, slugify_company(target.company)

    def test_button_appears_on_source_detail(self, client):
        _, slug = self._pick_employer()
        body = client.get(f"/sources/{slug}").data.decode()
        assert "Add to outreach" in body
        # The form points at the correct action URL.
        assert f"/sources/{slug}/add-to-outreach" in body

    def test_post_adds_entry_to_outreach_config(self, client, tmp_path, monkeypatch):
        target, slug = self._pick_employer()
        outreach_path = tmp_path / "outreach_companies.json"
        monkeypatch.setattr(
            "src.outreach.targets.OUTREACH_CONFIG_PATH", outreach_path,
        )

        resp = client.post(f"/sources/{slug}/add-to-outreach", follow_redirects=True)
        assert resp.status_code == 200
        assert f"{target.company} added to outreach" in resp.data.decode()
        rows = json.loads(outreach_path.read_text())
        assert any(r["company"] == target.company for r in rows)

    def test_double_click_reports_already_added_not_duplicate(self, client, tmp_path, monkeypatch):
        target, slug = self._pick_employer()
        outreach_path = tmp_path / "outreach_companies.json"
        monkeypatch.setattr(
            "src.outreach.targets.OUTREACH_CONFIG_PATH", outreach_path,
        )

        client.post(f"/sources/{slug}/add-to-outreach")
        second = client.post(f"/sources/{slug}/add-to-outreach", follow_redirects=True)
        rows = json.loads(outreach_path.read_text())
        assert len(rows) == 1
        assert "already on the outreach list" in second.data.decode()

    def test_unknown_slug_is_404(self, client):
        resp = client.post("/sources/definitely-not-a-real-employer-zzz/add-to-outreach")
        assert resp.status_code == 404
