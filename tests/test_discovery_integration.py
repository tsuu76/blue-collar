"""
Tests for the discovery integration surface: the dashboard's /api/discover
JSON endpoint (what n8n calls), the /discover button route, and the
browser-assisted hand-off. No real network, no real browser launch.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.browser_assist.handoff import BrowserAutomationDisabledError, open_application_page
from src.dashboard.app import create_app
from src.database.db import init_db


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "discovery_integration.db"
    init_db(path)
    return path


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestApiDiscoverEndpoint:
    def test_returns_json_counts(self, client):
        fake_result = {"employers_checked": 1, "found": 5, "inserted": 3, "duplicates": 2, "pipeline": {"ready_to_apply": 1}}
        with patch("src.job_discovery.run_discovery.run_discovery", return_value=fake_result):
            resp = client.post("/api/discover", json={})
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["ok"] is True
        assert body["inserted"] == 3
        assert body["duplicates"] == 2

    def test_process_false_is_passed_through(self, client, db_path):
        with patch("src.job_discovery.run_discovery.run_discovery") as mock_run:
            mock_run.return_value = {"employers_checked": 0, "found": 0, "inserted": 0, "duplicates": 0}
            client.post("/api/discover", json={"process": False})
        assert mock_run.call_args.kwargs["process"] is False

    def test_defaults_to_process_true(self, client):
        with patch("src.job_discovery.run_discovery.run_discovery") as mock_run:
            mock_run.return_value = {"employers_checked": 0, "found": 0, "inserted": 0, "duplicates": 0}
            client.post("/api/discover", json={})
        assert mock_run.call_args.kwargs["process"] is True

    def test_failure_returns_json_error_not_html(self, client):
        with patch("src.job_discovery.run_discovery.run_discovery", side_effect=RuntimeError("boom")):
            resp = client.post("/api/discover", json={})
        assert resp.status_code == 500
        body = json.loads(resp.data)  # must be JSON, not an HTML error page — n8n needs to parse this
        assert body["ok"] is False
        assert "boom" in body["error"]

    def test_handles_missing_json_body(self, client):
        with patch("src.job_discovery.run_discovery.run_discovery") as mock_run:
            mock_run.return_value = {"employers_checked": 0, "found": 0, "inserted": 0, "duplicates": 0}
            resp = client.post("/api/discover")
        assert resp.status_code == 200


class TestDiscoverButtonRoute:
    def test_redirects_to_dashboard_with_summary(self, client):
        fake_result = {
            "employers_checked": 1,
            "found": 5,
            "inserted": 3,
            "duplicates": 2,
            "pipeline": {"ready_to_apply": 1, "rejected": 2},
        }
        with patch("src.job_discovery.run_discovery.run_discovery", return_value=fake_result):
            resp = client.post("/discover", follow_redirects=True)
        assert resp.status_code == 200
        assert b"inserted 3" in resp.data
        assert b"ready to apply" in resp.data

    def test_failure_flashes_message_not_500(self, client):
        with patch("src.job_discovery.run_discovery.run_discovery", side_effect=RuntimeError("network down")):
            resp = client.post("/discover", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Discovery failed" in resp.data

    def test_discover_button_present_on_dashboard(self, client):
        resp = client.get("/")
        assert b"Run discovery" in resp.data


class TestBrowserHandoff:
    def test_raises_when_browser_automation_disabled(self):
        import dataclasses

        from src.config import settings as real_settings

        disabled = dataclasses.replace(real_settings, allow_browser_automation=False)
        with patch("src.browser_assist.handoff.settings", disabled):
            with pytest.raises(BrowserAutomationDisabledError):
                open_application_page("https://example.com/apply/1")

    def test_opens_page_and_reports_attachments_when_enabled(self):
        import dataclasses

        from src.config import settings as real_settings

        enabled = dataclasses.replace(real_settings, allow_browser_automation=True)
        with patch("src.browser_assist.handoff.settings", enabled):
            with patch("playwright.sync_api.sync_playwright") as mock_pw:
                result = open_application_page(
                    "https://example.com/apply/1",
                    resume_path="/tmp/resume.pdf",
                    cover_letter_path="/tmp/cover-letter.pdf",
                )
        assert result.opened is True
        assert "/tmp/resume.pdf" in result.attachments
        assert "/tmp/cover-letter.pdf" in result.attachments

    def test_empty_url_returns_not_opened(self):
        import dataclasses

        from src.config import settings as real_settings

        enabled = dataclasses.replace(real_settings, allow_browser_automation=True)
        with patch("src.browser_assist.handoff.settings", enabled):
            result = open_application_page("")
        assert result.opened is False

    def test_handoff_never_fills_or_submits(self):
        # Guard against future "helpful" additions: the hand-off module must
        # not reference any form-filling/submitting Playwright API. This is a
        # deliberate scope boundary agreed with the user, not an oversight.
        from pathlib import Path

        source = Path("src/browser_assist/handoff.py").read_text()
        for forbidden in (".fill(", ".set_input_files(", ".click(", ".type(", ".press("):
            assert forbidden not in source, f"handoff.py must not use {forbidden} — it never interacts with forms"
