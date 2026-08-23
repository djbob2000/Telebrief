"""Tests for Facebook browser profile persistence, security, and state classification (Plan 5 Task 2)."""

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.providers.facebook.auth import (
    FacebookAuthState,
    FacebookHumanActionRequired,
    classify_facebook_page_state,
    classify_facebook_url_and_content,
    ensure_owner_only_directory,
    resolve_profile_dir,
)


class TestFacebookProfileSecurity:
    """Security tests for profile paths and filesystem permissions."""

    def test_rejects_path_traversal(self, tmp_path: Path):
        auth_root = tmp_path / "auth"
        auth_root.mkdir()

        with pytest.raises(ValueError, match="invalid Facebook auth storage_ref"):
            resolve_profile_dir(auth_root, "../outside")

        with pytest.raises(ValueError, match="invalid Facebook auth storage_ref"):
            resolve_profile_dir(auth_root, "/etc/passwd")

        with pytest.raises(ValueError, match="invalid Facebook auth storage_ref"):
            resolve_profile_dir(auth_root, "foo/../../bar")

    def test_resolves_valid_relative_path(self, tmp_path: Path):
        auth_root = tmp_path / "auth"
        auth_root.mkdir()

        resolved = resolve_profile_dir(auth_root, "user1/profile_default")
        assert resolved == (auth_root / "user1" / "profile_default").resolve()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
    def test_ensure_owner_only_directory(self, tmp_path: Path):
        target = tmp_path / "secure_auth_profile"
        ensure_owner_only_directory(target)

        assert target.exists()
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o700


class TestFacebookSessionClassification:
    """Conservative session-state classification tests."""

    @pytest.mark.parametrize(
        ("url", "content", "expected"),
        [
            ("https://www.facebook.com/login", "", FacebookAuthState.AUTH_REQUIRED),
            ("https://www.facebook.com/login.php", "", FacebookAuthState.AUTH_REQUIRED),
            (
                "https://www.facebook.com/",
                "Log into Facebook to continue",
                FacebookAuthState.AUTH_REQUIRED,
            ),
            ("https://www.facebook.com/checkpoint/123", "", FacebookAuthState.CHECKPOINT_REQUIRED),
            (
                "https://www.facebook.com/security/2fa",
                "Two-Factor Authentication required",
                FacebookAuthState.CHECKPOINT_REQUIRED,
            ),
            ("https://www.facebook.com/captcha", "", FacebookAuthState.ACCOUNT_ACTION_REQUIRED),
            (
                "https://www.facebook.com/home",
                "Security Check: Enter the characters you see",
                FacebookAuthState.ACCOUNT_ACTION_REQUIRED,
            ),
            (
                "https://www.facebook.com/groups/berdyansk_news",
                "Welcome to Berdyansk News Group",
                FacebookAuthState.READY,
            ),
        ],
    )
    def test_classify_url_and_content(self, url: str, content: str, expected: FacebookAuthState):
        assert classify_facebook_url_and_content(url, content) == expected

    @pytest.mark.asyncio
    async def test_classify_page_state_with_mock_page(self):
        page = MagicMock()
        page.url = "https://www.facebook.com/checkpoint"
        page.content = AsyncMock(return_value="<html><body>Approve your login</body></html>")

        state = await classify_facebook_page_state(page)
        assert state == FacebookAuthState.CHECKPOINT_REQUIRED

    def test_human_action_required_exception_has_clean_message(self):
        exc = FacebookHumanActionRequired(FacebookAuthState.CHECKPOINT_REQUIRED)
        assert str(exc) == "Facebook interactive action required: checkpoint_required"
        assert exc.state == FacebookAuthState.CHECKPOINT_REQUIRED


class TestFacebookBrowserSession:
    """Tests for browser context manager lifecycle and error cleanup."""

    @pytest.mark.asyncio
    async def test_stops_playwright_if_launch_fails(self, tmp_path: Path):
        from unittest.mock import patch

        from src.providers.facebook.browser import FacebookBrowserSession
        from src.providers.facebook.models import FacebookAuthProfile

        profile = FacebookAuthProfile(
            id=1,
            name="test-profile",
            storage_ref="test-profile",
            status="ready",
        )
        session = FacebookBrowserSession(auth_root=tmp_path, profile=profile)

        mock_pw = MagicMock()
        mock_pw.stop = AsyncMock()
        mock_pw.chromium.launch_persistent_context = AsyncMock(
            side_effect=RuntimeError("Chromium binary missing")
        )

        with patch("src.providers.facebook.browser.async_playwright") as mock_ap:
            mock_ap_builder = MagicMock()
            mock_ap_builder.start = AsyncMock(return_value=mock_pw)
            mock_ap.return_value = mock_ap_builder

            with pytest.raises(RuntimeError, match="Chromium binary missing"):
                async with session:
                    pass

        mock_pw.stop.assert_awaited_once()
        assert session._playwright is None

    @pytest.mark.asyncio
    async def test_disabled_profile_raises_before_playwright_starts(self, tmp_path: Path):
        from unittest.mock import patch

        from src.providers.facebook.auth import FacebookHumanActionRequired
        from src.providers.facebook.browser import FacebookBrowserSession
        from src.providers.facebook.models import FacebookAuthProfile

        profile = FacebookAuthProfile(
            id=1,
            name="disabled-profile",
            storage_ref="disabled-profile",
            status="disabled",
        )
        session = FacebookBrowserSession(auth_root=tmp_path, profile=profile)

        with patch("src.providers.facebook.browser.async_playwright") as mock_ap:
            with pytest.raises(FacebookHumanActionRequired, match="circuit breaker tripped"):
                async with session:
                    pass
            mock_ap.assert_not_called()
