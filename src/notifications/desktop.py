"""
macOS desktop notifications via `osascript` — built into every Mac, no
dependency, no network call, no cost. Spec section 22 lists this as the
default/expected notification method since the system "primarily runs on
the user's Mac/laptop".

On any non-macOS platform this cleanly no-ops (logs a warning) rather than
crashing — notifications are a nice-to-have, never something the pipeline
should fail over.
"""
from __future__ import annotations

import logging
import platform
import subprocess

logger = logging.getLogger("job_hunter.notifications.desktop")


def _escape_applescript_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_desktop_notification(title: str, message: str, *, subtitle: str = "") -> bool:
    """Returns True if the notification command ran successfully."""
    if platform.system() != "Darwin":
        logger.warning("Desktop notifications are only implemented for macOS (osascript); skipping.")
        return False

    script_parts = [f'display notification "{_escape_applescript_string(message)}"']
    script_parts.append(f'with title "{_escape_applescript_string(title)}"')
    if subtitle:
        script_parts.append(f'subtitle "{_escape_applescript_string(subtitle)}"')
    script = " ".join(script_parts)

    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Desktop notification failed: %s", exc)
        return False
