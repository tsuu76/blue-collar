"""
Browser-assisted application hand-off.

Deliberately conservative, and this scope was explicitly chosen with the
user rather than assumed: this opens the real application page in a real,
VISIBLE browser window and surfaces the generated resume/cover-letter file
paths so they're ready to attach. It does NOT fill fields, does NOT upload
files, and does NOT submit — the human does all form interaction.

Why not automated field-filling, even "stopping before submit":
  - Heuristic field detection (matching label text) mis-maps on real forms
    that can't all be tested against, and a silently mis-filled application
    is worse than no automation.
  - Automated form interaction — even without submitting — is more likely
    to trip an ATS's bot detection than simply loading a page a human then
    drives themselves.
  - The spec's hard rule (never bypass anti-bot/CAPTCHA/login protections)
    is satisfied most robustly by not automating the form at all.

So every job remains TYPE_B/manual in the database (see
src/browser_assist/classify.py) — this is purely a convenience launcher
for a human-driven application, not an automation path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.config import settings

logger = logging.getLogger("job_hunter.browser_assist.handoff")


class BrowserAutomationDisabledError(RuntimeError):
    """Raised when a hand-off is attempted while ALLOW_BROWSER_AUTOMATION is false."""


@dataclass
class HandoffResult:
    opened: bool
    application_url: str
    attachments: list[str] = field(default_factory=list)
    message: str = ""


def open_application_page(
    application_url: str,
    *,
    resume_path: str | None = None,
    cover_letter_path: str | None = None,
    keep_open_seconds: int = 0,
) -> HandoffResult:
    """
    Open `application_url` in a visible browser for the user to complete
    themselves, and report which generated files are ready to attach.

    Gated behind ALLOW_BROWSER_AUTOMATION (default false) — even though
    this only opens a page, it's still the project's browser-automation
    entry point, so it respects the same explicit opt-in switch. Raises
    BrowserAutomationDisabledError when disabled, so a caller can surface
    a clear "turn this on in .env first" message rather than silently
    doing nothing.

    NOTE: this never fills, uploads, or submits anything.
    """
    if not settings.allow_browser_automation:
        raise BrowserAutomationDisabledError(
            "Browser assist is disabled. Set ALLOW_BROWSER_AUTOMATION=true in .env to enable it. "
            "Even when enabled, this only opens the application page for you — it never fills or submits a form."
        )

    if not application_url:
        return HandoffResult(opened=False, application_url="", message="No application URL for this job.")

    attachments = [p for p in (resume_path, cover_letter_path) if p]

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # headless=False deliberately: the whole point is that a human sees
        # and drives this window. A headless hand-off would be useless.
        browser = p.chromium.launch(headless=False)
        try:
            page = browser.new_page()
            page.goto(application_url, wait_until="domcontentloaded")
            if keep_open_seconds:
                page.wait_for_timeout(keep_open_seconds * 1000)
        finally:
            browser.close()

    logger.info("Opened application page for human completion: %s", application_url)
    return HandoffResult(
        opened=True,
        application_url=application_url,
        attachments=attachments,
        message="Application page opened. Attach the generated files and complete the form yourself.",
    )
