"""
Tests for Phase 16: application-type classification, platform labeling,
robots.txt compliance, and link reachability. No real network calls —
robots.txt/HTTP fetches are mocked throughout.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from src.browser_assist.classify import classify_application_type, detect_platform
from src.browser_assist.reachability import check_url_reachable
from src.browser_assist.robots import is_allowed_by_robots


class TestClassifyApplicationType:
    def test_always_returns_type_b(self):
        # By design — see module docstring. No URL, however automation-
        # friendly-looking, should ever produce TYPE_A without verified
        # per-site policy review, which can't be done in code.
        assert classify_application_type("https://myworkdayjobs.com/apply/123") == "TYPE_B"
        assert classify_application_type("https://example.com/careers/apply") == "TYPE_B"
        assert classify_application_type("") == "TYPE_B"


class TestDetectPlatform:
    def test_detects_known_platforms(self):
        assert detect_platform("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123") == "Workday"
        assert detect_platform("https://boards.greenhouse.io/acme/jobs/123") == "Greenhouse"
        assert detect_platform("https://jobs.lever.co/acme/123") == "Lever"
        assert detect_platform("https://www.seek.com.au/job/123") == "SEEK"

    def test_unrecognized_platform_returns_none(self):
        assert detect_platform("https://example-startup.com/careers/apply") is None

    def test_malformed_url_returns_none_not_raise(self):
        assert detect_platform("not a url at all :::") is None

    def test_empty_url_returns_none(self):
        assert detect_platform("") is None


class TestRobotsCompliance:
    def test_allowed_when_robots_permits(self):
        with patch("src.browser_assist.robots.robotparser.RobotFileParser") as MockParser:
            instance = MockParser.return_value
            instance.can_fetch.return_value = True
            result = is_allowed_by_robots("https://example.com/jobs/1")
        assert result is True

    def test_disallowed_when_robots_forbids(self):
        with patch("src.browser_assist.robots.robotparser.RobotFileParser") as MockParser:
            instance = MockParser.return_value
            instance.can_fetch.return_value = False
            result = is_allowed_by_robots("https://example.com/private/1")
        assert result is False

    def test_fails_open_when_robots_txt_unreadable(self):
        with patch("src.browser_assist.robots.robotparser.RobotFileParser") as MockParser:
            instance = MockParser.return_value
            instance.read.side_effect = Exception("connection refused")
            result = is_allowed_by_robots("https://example.com/jobs/1")
        assert result is True  # missing/unreadable robots.txt is not a disallow

    def test_invalid_url_returns_false(self):
        assert is_allowed_by_robots("not-a-url") is False


class TestReachability:
    def test_reachable_200(self):
        with patch("src.browser_assist.reachability.is_allowed_by_robots", return_value=True):
            with patch("src.browser_assist.reachability.requests.head") as mock_head:
                mock_head.return_value = MagicMock(status_code=200)
                result = check_url_reachable("https://example.com/jobs/1")
        assert result.reachable is True
        assert result.status_code == 200

    def test_unreachable_404(self):
        with patch("src.browser_assist.reachability.is_allowed_by_robots", return_value=True):
            with patch("src.browser_assist.reachability.requests.head") as mock_head:
                mock_head.return_value = MagicMock(status_code=404)
                result = check_url_reachable("https://example.com/jobs/expired")
        assert result.reachable is False
        assert result.status_code == 404

    def test_falls_back_to_get_when_head_not_allowed(self):
        with patch("src.browser_assist.reachability.is_allowed_by_robots", return_value=True):
            with patch("src.browser_assist.reachability.requests.head") as mock_head, patch(
                "src.browser_assist.reachability.requests.get"
            ) as mock_get:
                mock_head.return_value = MagicMock(status_code=405)
                mock_get.return_value = MagicMock(status_code=200)
                result = check_url_reachable("https://example.com/jobs/1")
        assert result.reachable is True
        mock_get.assert_called_once()

    def test_respects_robots_disallow(self):
        with patch("src.browser_assist.reachability.is_allowed_by_robots", return_value=False):
            with patch("src.browser_assist.reachability.requests.head") as mock_head:
                result = check_url_reachable("https://example.com/private/1")
        assert result.reachable is False
        assert result.robots_disallowed is True
        mock_head.assert_not_called()

    def test_network_error_returns_unreachable_not_raise(self):
        with patch("src.browser_assist.reachability.is_allowed_by_robots", return_value=True):
            with patch("src.browser_assist.reachability.requests.head", side_effect=requests.RequestException("timeout")):
                result = check_url_reachable("https://example.com/jobs/1")
        assert result.reachable is False
        assert result.error is not None
