"""Runtime enablement policy for Facebook provider components."""

from __future__ import annotations

from typing import Any


def is_facebook_enabled(config: Any = None) -> bool:
    """Return True if Facebook integration is enabled in config or runtime settings."""
    if config is None:
        try:
            from src.runtime import get_runtime

            runtime = get_runtime()
            config = getattr(runtime, "config", None)
        except Exception:
            return False

    if config is None:
        return False

    facebook_cfg = getattr(config, "facebook", None)
    if facebook_cfg is None:
        if isinstance(config, dict):
            facebook_cfg = config.get("facebook")
            if isinstance(facebook_cfg, dict):
                return bool(facebook_cfg.get("enabled", False))
        return False

    return bool(getattr(facebook_cfg, "enabled", False))
