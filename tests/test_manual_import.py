"""
Tests for the manual job import path (Phase 6). This is the always-available
Type B intake mechanism, since automated SEEK/Indeed scraping is out of
scope per the anti-bot/ToS constraints.
"""
from __future__ import annotations

import pytest

from src.sources.manual_import import normalize_manual_job, parse_pasted_text


class TestNormalizeManualJob:
    def test_builds_normalized_job(self):
        job = normalize_manual_job(
            title="IT Support Officer",
            description="Entry-level service desk role.",
            url="https://example.com/jobs/1",
            company="Acme Pty Ltd",
            location="Sydney NSW",
        )
        assert job.title == "IT Support Officer"
        assert job.company == "Acme Pty Ltd"
        assert job.source == "manual_paste"

    def test_strips_whitespace(self):
        job = normalize_manual_job(
            title="  IT Support Officer  ",
            description="  Some description.  ",
            company="  Acme  ",
        )
        assert job.title == "IT Support Officer"
        assert job.company == "Acme"

    def test_missing_title_raises(self):
        with pytest.raises(ValueError):
            normalize_manual_job(title="", description="Some description")

    def test_missing_description_raises(self):
        with pytest.raises(ValueError):
            normalize_manual_job(title="IT Support Officer", description="")

    def test_to_dict_round_trip(self):
        job = normalize_manual_job(
            title="IT Support Officer",
            description="Entry-level service desk role.",
            url="https://example.com/jobs/1",
        )
        d = job.to_dict()
        assert d["title"] == "IT Support Officer"
        assert d["url"] == "https://example.com/jobs/1"
        assert d["source"] == "manual_paste"


class TestParsePastedText:
    def test_extracts_title_from_first_line(self):
        text = "IT Support Officer\n\nWe are looking for an entry-level IT Support Officer..."
        job = parse_pasted_text(text)
        assert job.title == "IT Support Officer"

    def test_extracts_url_from_text(self):
        text = "IT Support Officer\n\nApply at https://example.com/jobs/123 before Friday."
        job = parse_pasted_text(text)
        assert job.url == "https://example.com/jobs/123"

    def test_explicit_source_url_takes_priority(self):
        text = "IT Support Officer\n\nApply at https://example.com/jobs/123 before Friday."
        job = parse_pasted_text(text, source_url="https://example.com/canonical")
        assert job.url == "https://example.com/canonical"

    def test_does_not_guess_company_or_location(self):
        text = "IT Support Officer\n\nSome description with no clear company line."
        job = parse_pasted_text(text)
        assert job.company == ""
        assert job.location == ""

    def test_long_first_line_not_treated_as_title(self):
        text = (
            "This is a very long first line that reads like a sentence describing the role "
            "in detail rather than being a short job title, so it should not be used as one.\n\n"
            "More description follows here."
        )
        job = parse_pasted_text(text)
        assert job.title == ""

    def test_empty_text_raises(self):
        with pytest.raises(ValueError):
            parse_pasted_text("   \n\n   ")

    def test_description_preserves_full_text(self):
        text = "IT Support Officer\n\nFull description body here."
        job = parse_pasted_text(text)
        assert "Full description body here." in job.description
