"""
Tests for the outreach target registry loader.

Every company name here is a synthetic fixture written for this file.
"""
from __future__ import annotations

import json

import pytest

from src.outreach.targets import OutreachTarget, enabled_targets, load_targets


@pytest.fixture()
def write_config(tmp_path):
    def _write(entries):
        path = tmp_path / "outreach_companies.json"
        path.write_text(json.dumps(entries))
        return path

    return _write


class TestLoadTargets:
    def test_loads_a_valid_entry(self, write_config):
        path = write_config([
            {
                "company": "Example Co",
                "website": "https://example.invalid",
                "platform": "greenhouse",
                "identifier": "exampleco",
                "contact_email": "Careers@Example.invalid",
                "notes": "Sydney MSP",
            }
        ])
        targets = load_targets(path)
        assert len(targets) == 1
        target = targets[0]
        assert target.company == "Example Co"
        assert target.platform == "greenhouse"
        assert target.contact_email == "careers@example.invalid"  # normalized
        assert target.enabled is True
        assert target.has_job_board() is True

    def test_missing_file_returns_empty(self, tmp_path):
        """An unconfigured install has nothing to contact, not a crash."""
        assert load_targets(tmp_path / "nope.json") == []

    def test_malformed_json_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert load_targets(path) == []

    def test_non_array_returns_empty(self, write_config):
        path = write_config({"company": "Example Co"})
        assert load_targets(path) == []

    def test_skips_entry_without_a_company_name(self, write_config):
        path = write_config([{"website": "https://example.invalid"}, {"company": "Good Co"}])
        assert [t.company for t in load_targets(path)] == ["Good Co"]

    def test_skips_non_object_entry(self, write_config):
        path = write_config(["just a string", {"company": "Good Co"}])
        assert [t.company for t in load_targets(path)] == ["Good Co"]

    def test_one_bad_entry_does_not_lose_the_others(self, write_config):
        path = write_config([{"company": "A Co"}, {"nope": True}, {"company": "B Co"}])
        assert [t.company for t in load_targets(path)] == ["A Co", "B Co"]

    def test_unsupported_platform_is_dropped_not_used(self, write_config):
        """An unsupported ATS must never be passed to an adapter lookup."""
        path = write_config([
            {"company": "Example Co", "platform": "pageup", "identifier": "whatever"}
        ])
        target = load_targets(path)[0]
        assert target.platform == ""
        assert target.identifier == ""
        assert target.has_job_board() is False

    def test_platform_is_normalized(self, write_config):
        path = write_config([{"company": "Example Co", "platform": "GREENHOUSE", "identifier": "x"}])
        assert load_targets(path)[0].platform == "greenhouse"

    def test_defaults_are_safe(self, write_config):
        target = load_targets(write_config([{"company": "Bare Co"}]))[0]
        assert target.website == ""
        assert target.contact_email == ""
        assert target.platform == ""
        assert target.enabled is True
        assert target.has_job_board() is False


class TestEnabledTargets:
    def test_filters_disabled(self, write_config):
        path = write_config([
            {"company": "On Co", "enabled": True},
            {"company": "Off Co", "enabled": False},
        ])
        assert [t.company for t in enabled_targets(path)] == ["On Co"]

    def test_empty_registry_is_empty(self, write_config):
        assert enabled_targets(write_config([])) == []


class TestCompanyRecord:
    def test_maps_to_repo_shape(self):
        target = OutreachTarget(
            company="Example Co",
            website="https://example.invalid",
            platform="lever",
            identifier="exampleco",
            contact_email="careers@example.invalid",
        )
        record = target.to_company_record()
        assert record["name"] == "Example Co"
        assert record["platform"] == "lever"
        assert record["contact_email"] == "careers@example.invalid"
        assert record["source"] == "config"
