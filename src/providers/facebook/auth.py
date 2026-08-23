"""Facebook session-state classification and persistent profile security (Plan 5 Task 2)."""

from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any


class FacebookAuthState(str, Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    CHECKPOINT_REQUIRED = "checkpoint_required"
    ACCOUNT_ACTION_REQUIRED = "account_action_required"
    DISABLED = "disabled"


class FacebookHumanActionRequired(Exception):
    """Raised when Facebook requires interactive operator intervention (login, checkpoint, captcha)."""

    def __init__(self, state: FacebookAuthState | str, message: str | None = None):
        self.state = FacebookAuthState(state) if isinstance(state, str) else state
        msg = message or f"Facebook interactive action required: {self.state.value}"
        super().__init__(msg)


def resolve_profile_dir(auth_root: Path | str, storage_ref: str) -> Path:
    """Resolve and validate profile directory inside auth_root."""
    root = Path(auth_root).resolve()
    relative = PurePosixPath(storage_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"invalid Facebook auth storage_ref: {storage_ref!r}")

    path = root.joinpath(*relative.parts).resolve()
    # Ensure resolved path is strictly within root
    try:
        path.relative_to(root)
    except ValueError as e:
        raise ValueError(
            f"Facebook auth storage_ref traverses outside auth_root: {storage_ref!r}"
        ) from e

    return path


def ensure_owner_only_directory(path: Path) -> None:
    """Create directory if missing and enforce 0700 permissions on POSIX systems."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def classify_facebook_url_and_content(url: str, html_or_text: str = "") -> FacebookAuthState:
    """Classify Facebook page state conservatively based on URL and page text."""
    url_lower = url.lower()
    text_lower = html_or_text.lower()

    # Checkpoint / 2FA check
    if any(
        k in url_lower
        for k in ["/checkpoint", "checkpoint/", "two_factor", "two_step_verification"]
    ):
        return FacebookAuthState.CHECKPOINT_REQUIRED
    if any(
        k in text_lower
        for k in ["two-factor authentication", "security code", "approve your login"]
    ):
        return FacebookAuthState.CHECKPOINT_REQUIRED

    # Account action / Captcha / suspension / bot detection check
    if any(k in url_lower for k in ["captcha", "recaptcha", "temporary_block", "account_locked"]):
        return FacebookAuthState.ACCOUNT_ACTION_REQUIRED
    if any(
        k in text_lower
        for k in [
            "enter the characters you see",
            "security check",
            "your account has been locked",
            "we disabled your account",
            "confirm your identity",
            "temporarily locked",
            "unusual activity",
        ]
    ):
        return FacebookAuthState.ACCOUNT_ACTION_REQUIRED

    # Login / Auth required check
    if any(k in url_lower for k in ["/login", "login.php", "/recover"]):
        return FacebookAuthState.AUTH_REQUIRED
    if any(
        k in text_lower
        for k in [
            "log into facebook",
            "log in to facebook",
            "email or phone number",
            "forgot password?",
            "create new account",
        ]
    ):
        return FacebookAuthState.AUTH_REQUIRED

    return FacebookAuthState.READY


logger = logging.getLogger(__name__)


async def classify_facebook_page_state(page: Any) -> FacebookAuthState:
    """Inspect Playwright page object and return classified session state."""
    try:
        url = page.url or ""
    except Exception as e:
        logger.debug("Failed reading page.url: %s", e)
        url = ""

    text = ""
    try:
        text = await page.content()
    except Exception as e:
        logger.debug("Failed reading page.content(): %s", e)

    return classify_facebook_url_and_content(url, text)
