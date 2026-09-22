"""
Tests for src/outreach/relevance.py — the AU relevance summary for
outreach research. Uses the same location-matching rule as the
job-hunt filter stage, so drift between the two would show up here.
"""
from __future__ import annotations

from src.outreach.relevance import LocationRelevance, au_relevance


class TestAuRelevance:
    def test_all_target_locations_match(self):
        result = au_relevance(
            ["Sydney NSW", "Remote Australia", "Hybrid Sydney"],
            target_locations=["Sydney", "NSW", "Remote Australia", "Hybrid Sydney"],
        )
        assert result == LocationRelevance(matching=3, total=3)
        assert result.any_match is True

    def test_no_target_matches(self):
        result = au_relevance(
            ["Toronto", "Berlin", "Singapore"],
            target_locations=["Sydney", "NSW", "Remote Australia"],
        )
        assert result == LocationRelevance(matching=0, total=3)
        assert result.any_match is False

    def test_mixed(self):
        result = au_relevance(
            ["Sydney NSW", "Toronto", "Sydney (Hybrid)"],
            target_locations=["Sydney", "NSW"],
        )
        assert result.matching == 2
        assert result.total == 3

    def test_case_insensitive(self):
        result = au_relevance(
            ["sydney nsw", "SYDNEY"],
            target_locations=["Sydney"],
        )
        assert result.matching == 2

    def test_empty_locations_count_toward_total_but_never_match(self):
        result = au_relevance(
            ["", "Sydney NSW", "  "],
            target_locations=["Sydney"],
        )
        assert result.total == 3
        assert result.matching == 1

    def test_empty_input_returns_zero_zero(self):
        result = au_relevance([], target_locations=["Sydney"])
        assert result == LocationRelevance(0, 0)
        assert result.any_match is False

    def test_defaults_to_settings_target_locations(self, monkeypatch):
        # No explicit target_locations — the function reads settings at
        # call time, so a test that swaps settings sees the new list.
        import dataclasses

        from src import config

        swapped = dataclasses.replace(config.settings, target_locations=["Melbourne"])
        monkeypatch.setattr("src.outreach.relevance.settings", swapped)
        result = au_relevance(["Sydney NSW", "Melbourne VIC"])
        assert result.matching == 1

    def test_to_dict_shape(self):
        assert au_relevance(["Sydney NSW"], target_locations=["Sydney"]).to_dict() == {
            "matching": 1,
            "total": 1,
        }
