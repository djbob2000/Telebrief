#!/usr/bin/env python3
"""Interactive bootstrap script for Facebook persistent browser profiles (Plan 5 Task 2).

Usage:
    python scripts/bootstrap_facebook_profile.py --profile default --auth-root /var/lib/telebrief/auth
    python scripts/bootstrap_facebook_profile.py --profile default --import-cookies cookies.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright

from src.providers.facebook.auth import (
    FacebookAuthState,
    classify_facebook_page_state,
    ensure_owner_only_directory,
    resolve_profile_dir,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def bootstrap_profile(
    *,
    auth_root: str,
    storage_ref: str,
    import_cookies_path: str | None = None,
    discard_cookies_file: bool = False,
) -> None:
    profile_path = resolve_profile_dir(auth_root, storage_ref)
    ensure_owner_only_directory(profile_path)
    logger.info("Opening browser profile at %s", profile_path)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )

        if import_cookies_path:
            cookie_file = Path(import_cookies_path)
            if not cookie_file.exists():
                raise FileNotFoundError(f"Cookies file not found: {import_cookies_path}")
            logger.info("Importing cookies from %s", import_cookies_path)
            with cookie_file.open("r", encoding="utf-8") as f:
                cookies = json.load(f)
                if isinstance(cookies, list):
                    await context.add_cookies(cookies)
                else:
                    logger.warning("Cookies file did not contain a JSON list, skipping import")

            if discard_cookies_file:
                logger.info("Discarding raw cookies file %s as requested", import_cookies_path)
                cookie_file.unlink()

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded")

        logger.info("Please log in to Facebook in the opened browser window.")
        logger.info("Once logged in, verify you are on the home feed or group page.")

        # Monitor state periodically
        while True:
            await asyncio.sleep(3)
            state = await classify_facebook_page_state(page)
            if state == FacebookAuthState.READY:
                logger.info("✅ Facebook session verified as READY!")
                break
            logger.info("Current session state: %s. Waiting for login completion...", state.value)

        await context.close()
        logger.info("Browser session saved successfully in %s", profile_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Facebook persistent browser profile")
    parser.add_argument(
        "--profile", default="default", help="Profile storage ref or name (default: default)"
    )
    parser.add_argument(
        "--auth-root", default="data/auth/facebook", help="Path to auth root directory"
    )
    parser.add_argument(
        "--import-cookies", default=None, help="Optional JSON file with exported cookies"
    )
    parser.add_argument(
        "--discard-cookies-file",
        action="store_true",
        help="Delete raw cookies file after successful import",
    )

    args = parser.parse_args()
    asyncio.run(
        bootstrap_profile(
            auth_root=args.auth_root,
            storage_ref=args.profile,
            import_cookies_path=args.import_cookies,
            discard_cookies_file=args.discard_cookies_file,
        )
    )


if __name__ == "__main__":
    main()
