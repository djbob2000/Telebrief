"""Playwright persistent browser context manager for Facebook (Plan 5 Task 2).

Uses standard Chromium persistent user data directories with operator-assisted
profile bootstrapping to persist session cookies and authentication state.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from src.providers.facebook.auth import (
    FacebookAuthState,
    FacebookHumanActionRequired,
    classify_facebook_page_state,
    ensure_owner_only_directory,
    is_profile_runnable,
    resolve_profile_dir,
)
from src.providers.facebook.models import FacebookAuthProfile

logger = logging.getLogger(__name__)


class FacebookBrowserSession:
    """Manages persistent browser session for a Facebook auth profile."""

    def __init__(
        self,
        auth_root: Path | str,
        profile: FacebookAuthProfile,
        *,
        headless: bool = True,
        launch_args: list[str] | None = None,
    ) -> None:
        self.auth_root = auth_root
        self.profile = profile
        self.headless = headless
        self.launch_args = launch_args or [
            "--no-sandbox",
        ]
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> tuple[BrowserContext, Page]:
        if not is_profile_runnable(self.profile.status):
            state = self.profile.status or FacebookAuthState.DISABLED.value
            raise FacebookHumanActionRequired(
                state,
                f"Facebook auth profile '{self.profile.name}' has non-runnable status '{self.profile.status}'",
            )

        path = resolve_profile_dir(self.auth_root, self.profile.storage_ref)
        ensure_owner_only_directory(path)

        pw = await async_playwright().start()
        self._playwright = pw
        try:
            self._context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(path),
                headless=self.headless,
                args=self.launch_args,
                viewport={"width": 1280, "height": 800},
            )
            page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            return self._context, page
        except Exception:
            await pw.stop()
            self._playwright = None
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as e:
                logger.warning("Error closing persistent browser context: %s", e)
            self._context = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning("Error stopping playwright: %s", e)
            self._playwright = None


@asynccontextmanager
async def open_authenticated_session(
    auth_root: Path | str,
    profile: FacebookAuthProfile,
    *,
    headless: bool = True,
    verify_url: str = "https://www.facebook.com/",
) -> AsyncIterator[tuple[BrowserContext, Page]]:
    """Open persistent browser session and verify authentication status before yielding."""
    async with FacebookBrowserSession(auth_root, profile, headless=headless) as (context, page):
        # Navigate to verify URL
        try:
            await page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning("Facebook session navigation failed: %s", e)

        state = await classify_facebook_page_state(page)
        if state != FacebookAuthState.READY:
            raise FacebookHumanActionRequired(
                state, f"Facebook auth profile '{profile.name}' is in state '{state.value}'"
            )

        yield context, page
