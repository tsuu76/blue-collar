"""
Shared pytest fixtures. Notifications are disabled globally during test
runs — without this, TestFullSuccessPath in test_pipeline.py (and similar
tests) would pop a real macOS desktop notification on every test run,
since NOTIFY_DESKTOP defaults to true. Tests that specifically want to
verify notification behavior (tests/test_notifications.py) patch
src.notifications.notifier.settings/src.notifications.telegram.settings
directly and are unaffected by this.

Settings is a frozen dataclass, so its fields can't be mutated in place
(monkeypatch.setattr on the instance raises FrozenInstanceError) — instead
this builds one modified copy via dataclasses.replace() and patches the
module-level `settings` NAME in each consuming module to point at it.
"""
from __future__ import annotations

import dataclasses

import pytest

from src.config import settings


@pytest.fixture(autouse=True)
def _disable_notifications_during_tests(monkeypatch):
    quiet_settings = dataclasses.replace(settings, notify_desktop=False, notify_telegram=False)
    monkeypatch.setattr("src.notifications.notifier.settings", quiet_settings)
    monkeypatch.setattr("src.notifications.telegram.settings", quiet_settings)
