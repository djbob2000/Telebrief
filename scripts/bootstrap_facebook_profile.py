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

from src.config_loader import Config, load_config
from src.providers.facebook.auth import (
    FacebookAuthState,
    classify_facebook_page_state,
    ensure_owner_only_directory,
    resolve_profile_dir,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def resolve_configured_profile(profile_arg: str, config: Config | None) -> tuple[str, str]:
    """Return (name, storage_ref) resolved from config or defaulting to profile_arg."""
    if config and hasattr(config, "facebook") and config.facebook and hasattr(config.facebook, "auth_profiles"):
        for p in config.facebook.auth_profiles:
            if p.name == profile_arg or p.storage_ref == profile_arg:
                return p.name, p.storage_ref
    return profile_arg, profile_arg


async def bootstrap_profile(
    *,
    auth_root: str | None = None,
    profile_arg: str = "default",
    import_cookies_path: str | None = None,
    discard_cookies_file: bool = False,
) -> None:
    try:
        cfg = load_config()
    except Exception:
        cfg = None

    effective_auth_root = (
        auth_root
        or (
            getattr(cfg.facebook, "auth_root", None)
            if (cfg and hasattr(cfg, "facebook") and cfg.facebook)
            else None
        )
        or "data/auth/facebook"
    )

    profile_name, profile_storage_ref = resolve_configured_profile(profile_arg, cfg)
    profile_path = resolve_profile_dir(effective_auth_root, profile_storage_ref)
    ensure_owner_only_directory(profile_path)
    logger.info("Opening browser profile at %s (profile name: %s)", profile_path, profile_name)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 800},
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

        final_state = FacebookAuthState.UNKNOWN
        # Monitor state periodically
        while True:
            await asyncio.sleep(3)
            state = await classify_facebook_page_state(page)
            if state == FacebookAuthState.READY:
                logger.info("✅ Facebook session verified as READY!")
                final_state = state
                break
            if state in (
                FacebookAuthState.CHECKPOINT_REQUIRED,
                FacebookAuthState.ACCOUNT_ACTION_REQUIRED,
                FacebookAuthState.DISABLED,
            ):
                logger.warning("Facebook session encountered blocking state: %s", state.value)
                final_state = state
                break
            logger.info("Current session state: %s. Waiting for login completion...", state.value)

        await context.close()
        logger.info("Browser session saved successfully in %s", profile_path)

        # Update database status for the auth profile
        if cfg and cfg.database.enabled:
            try:
                import datetime as dt

                from src.db.pool import open_pool
                from src.repositories.facebook import FacebookRepository

                pool = await open_pool(cfg.database)
                async with pool.connection() as conn:
                    fb_repo = FacebookRepository()
                    prof = await fb_repo.get_auth_profile_by_name(conn, profile_name)
                    if prof is None:
                        prof = await fb_repo.get_or_create_auth_profile(
                            conn, name=profile_name, storage_ref=profile_storage_ref
                        )
                    db_status = (
                        "ready" if final_state == FacebookAuthState.READY else final_state.value
                    )
                    verified_at = (
                        dt.datetime.now(dt.timezone.utc)
                        if final_state == FacebookAuthState.READY
                        else None
                    )
                    await fb_repo.update_auth_profile_status(
                        conn,
                        profile_id=prof.id,
                        status=db_status,
                        verified_at=verified_at,
                    )
                    await conn.commit()
                await pool.close()
                logger.info(
                    "Updated database status for profile '%s' (id=%s) to '%s'",
                    profile_name,
                    prof.id,
                    db_status,
                )
            except Exception as exc:
                logger.error(
                    "Could not update database status for profile '%s': %s", profile_name, exc
                )
                raise SystemExit(1) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Facebook persistent browser profile")
    parser.add_argument(
        "--profile", default="default", help="Profile storage ref or name (default: default)"
    )
    parser.add_argument(
        "--auth-root",
        default=None,
        help="Path to auth root directory (defaults to cfg.facebook.auth_root or data/auth/facebook)",
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
            profile_arg=args.profile,
            import_cookies_path=args.import_cookies,
            discard_cookies_file=args.discard_cookies_file,
        )
    )


if __name__ == "__main__":
    main()
