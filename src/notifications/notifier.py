"""
Unified notification entry point. Fans out to whichever channels are
enabled in settings (desktop, Telegram — both individually optional, both
free). Every channel is wrapped so a notification failure is only ever
logged, never raised — sending a notification must never break the
pipeline that triggered it.
"""
from __future__ import annotations

import logging

from src.config import settings

from .desktop import send_desktop_notification
from .telegram import send_telegram_notification

logger = logging.getLogger("job_hunter.notifications")


def notify(title: str, message: str, *, subtitle: str = "") -> None:
    if settings.notify_desktop:
        try:
            send_desktop_notification(title, message, subtitle=subtitle)
        except Exception as exc:  # noqa: BLE001 — notifications must never crash the caller
            logger.warning("Desktop notification raised unexpectedly: %s", exc)

    if settings.notify_telegram:
        try:
            send_telegram_notification(f"{title}\n{message}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram notification raised unexpectedly: %s", exc)


def notify_job_ready(title: str, company: str, fit_score: int | None) -> None:
    score_text = f"{fit_score}/100" if fit_score is not None else "unscored"
    notify(
        "New job ready to apply",
        f"{title} at {company} ({score_text}) — open the dashboard to review.",
    )


def notify_job_flagged_for_review(title: str, company: str) -> None:
    notify(
        "Application flagged for manual review",
        f"{title} at {company} generated an application, but quality control found issues — check the dashboard.",
    )
