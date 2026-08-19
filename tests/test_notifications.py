"""
Tests for Phase 15 notifications. Desktop notifications are tested by
mocking subprocess.run (never actually pops a real macOS notification
during the test run); Telegram is tested by mocking requests.post (never
makes a real network call).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.notifications.desktop import send_desktop_notification
from src.notifications.notifier import notify, notify_job_flagged_for_review, notify_job_ready
from src.notifications.telegram import send_telegram_notification


class TestDesktopNotification:
    def test_sends_on_macos(self):
        with patch("src.notifications.desktop.platform.system", return_value="Darwin"):
            with patch("src.notifications.desktop.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = send_desktop_notification("Title", "Message")
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "osascript"

    def test_noops_on_non_macos(self):
        with patch("src.notifications.desktop.platform.system", return_value="Linux"):
            result = send_desktop_notification("Title", "Message")
        assert result is False

    def test_returns_false_on_subprocess_failure(self):
        import subprocess

        with patch("src.notifications.desktop.platform.system", return_value="Darwin"):
            with patch("src.notifications.desktop.subprocess.run", side_effect=subprocess.CalledProcessError(1, "osascript")):
                result = send_desktop_notification("Title", "Message")
        assert result is False

    def test_escapes_quotes_in_message(self):
        with patch("src.notifications.desktop.platform.system", return_value="Darwin"):
            with patch("src.notifications.desktop.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                send_desktop_notification('Title "quoted"', 'Message with "quotes" in it')
        script = mock_run.call_args[0][0][2]
        assert '\\"quoted\\"' in script
        assert '\\"quotes\\"' in script


class TestTelegramNotification:
    def test_disabled_by_default_does_not_send(self):
        with patch("src.notifications.telegram.settings") as mock_settings:
            mock_settings.notify_telegram = False
            with patch("src.notifications.telegram.requests.post") as mock_post:
                result = send_telegram_notification("test message")
        assert result is False
        mock_post.assert_not_called()

    def test_enabled_but_unconfigured_does_not_send(self):
        with patch("src.notifications.telegram.settings") as mock_settings:
            mock_settings.notify_telegram = True
            mock_settings.telegram_bot_token = ""
            mock_settings.telegram_chat_id = ""
            with patch("src.notifications.telegram.requests.post") as mock_post:
                result = send_telegram_notification("test message")
        assert result is False
        mock_post.assert_not_called()

    def test_enabled_and_configured_sends(self):
        with patch("src.notifications.telegram.settings") as mock_settings:
            mock_settings.notify_telegram = True
            mock_settings.telegram_bot_token = "fake-token"
            mock_settings.telegram_chat_id = "12345"
            with patch("src.notifications.telegram.requests.post") as mock_post:
                mock_post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
                result = send_telegram_notification("test message")
        assert result is True
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "fake-token" in url

    def test_network_failure_returns_false_not_raise(self):
        import requests

        with patch("src.notifications.telegram.settings") as mock_settings:
            mock_settings.notify_telegram = True
            mock_settings.telegram_bot_token = "fake-token"
            mock_settings.telegram_chat_id = "12345"
            with patch("src.notifications.telegram.requests.post", side_effect=requests.RequestException("timeout")):
                result = send_telegram_notification("test message")
        assert result is False


class TestNotifyDispatch:
    def test_notify_calls_desktop_when_enabled(self):
        with patch("src.notifications.notifier.settings") as mock_settings:
            mock_settings.notify_desktop = True
            mock_settings.notify_telegram = False
            with patch("src.notifications.notifier.send_desktop_notification") as mock_desktop:
                notify("Title", "Message")
        mock_desktop.assert_called_once()

    def test_notify_skips_desktop_when_disabled(self):
        with patch("src.notifications.notifier.settings") as mock_settings:
            mock_settings.notify_desktop = False
            mock_settings.notify_telegram = False
            with patch("src.notifications.notifier.send_desktop_notification") as mock_desktop:
                notify("Title", "Message")
        mock_desktop.assert_not_called()

    def test_notify_never_raises_even_if_channel_errors(self):
        with patch("src.notifications.notifier.settings") as mock_settings:
            mock_settings.notify_desktop = True
            mock_settings.notify_telegram = False
            with patch("src.notifications.notifier.send_desktop_notification", side_effect=RuntimeError("boom")):
                notify("Title", "Message")  # must not raise

    def test_notify_job_ready_includes_score(self):
        with patch("src.notifications.notifier.notify") as mock_notify:
            notify_job_ready("IT Support Officer", "Acme", 88)
        args = mock_notify.call_args[0]
        assert "88/100" in args[1]
        assert "IT Support Officer" in args[1]
        assert "Acme" in args[1]

    def test_notify_job_flagged_for_review(self):
        with patch("src.notifications.notifier.notify") as mock_notify:
            notify_job_flagged_for_review("IT Support Officer", "Acme")
        title, message = mock_notify.call_args[0]
        assert "manual review" in title.lower()
        assert "IT Support Officer" in message
        assert "Acme" in message
