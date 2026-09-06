"""Facebook configuration parsers."""

from __future__ import annotations

import re

from src.config.schemas.facebook import (
    FacebookAuthProfileBootstrap,
    FacebookCommentsConfig,
    FacebookConfig,
    FacebookSourceBootstrap,
)


def _parse_facebook_config(yaml_config: dict) -> FacebookConfig:
    """Parse and validate the optional top-level facebook: block."""
    raw = yaml_config.get("facebook")
    if raw is None:
        return FacebookConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'facebook' must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"facebook.enabled must be a bool, got {type(enabled).__name__}")

    editorial_enabled = raw.get("editorial_enabled", True)
    if not isinstance(editorial_enabled, bool):
        raise ValueError(
            f"facebook.editorial_enabled must be a bool, got {type(editorial_enabled).__name__}"
        )

    auth_root = raw.get("auth_root", "/var/lib/telebrief/auth")
    if not isinstance(auth_root, str) or not auth_root.strip():
        raise ValueError("facebook.auth_root must be a non-empty string")

    comments_raw = raw.get("comments", {})
    if not isinstance(comments_raw, dict):
        raise ValueError(f"facebook.comments must be a mapping, got {type(comments_raw).__name__}")

    include_replies = comments_raw.get("include_replies", True)
    if not isinstance(include_replies, bool):
        raise ValueError("facebook.comments.include_replies must be a bool")

    max_comments = comments_raw.get("max_comments_per_post", 500)
    if not isinstance(max_comments, int) or isinstance(max_comments, bool) or max_comments <= 0:
        raise ValueError("facebook.comments.max_comments_per_post must be a positive int")

    max_replies = comments_raw.get("max_replies_per_comment", 100)
    if not isinstance(max_replies, int) or isinstance(max_replies, bool) or max_replies <= 0:
        raise ValueError("facebook.comments.max_replies_per_comment must be a positive int")

    max_pages = comments_raw.get("max_pages_per_refresh", 20)
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages <= 0:
        raise ValueError("facebook.comments.max_pages_per_refresh must be a positive int")

    max_duration = comments_raw.get("max_duration_per_post_seconds", 120)
    if not isinstance(max_duration, int) or isinstance(max_duration, bool) or max_duration <= 0:
        raise ValueError("facebook.comments.max_duration_per_post_seconds must be a positive int")

    comments_config = FacebookCommentsConfig(
        include_replies=include_replies,
        max_comments_per_post=max_comments,
        max_replies_per_comment=max_replies,
        max_pages_per_refresh=max_pages,
        max_duration_per_post_seconds=max_duration,
    )

    raw_profiles = raw.get("auth_profiles", [])
    if not isinstance(raw_profiles, list):
        raise ValueError("facebook.auth_profiles must be a list")
    auth_profiles: list[FacebookAuthProfileBootstrap] = []
    for idx, p in enumerate(raw_profiles):
        if not isinstance(p, dict):
            raise ValueError(f"facebook.auth_profiles[{idx}] must be a mapping")
        name = p.get("name")
        storage_ref = p.get("storage_ref")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"facebook.auth_profiles[{idx}].name must be a non-empty string")
        if not isinstance(storage_ref, str) or not storage_ref.strip() or ".." in storage_ref:
            raise ValueError(
                f"facebook.auth_profiles[{idx}].storage_ref must be a valid relative path without '..'"
            )
        auth_profiles.append(
            FacebookAuthProfileBootstrap(name=name.strip(), storage_ref=storage_ref.strip())
        )

    raw_sources = raw.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError("facebook.sources must be a list")
    sources: list[FacebookSourceBootstrap] = []
    for idx, s in enumerate(raw_sources):
        if not isinstance(s, dict):
            raise ValueError(f"facebook.sources[{idx}] must be a mapping")
        name = s.get("name")
        kind = s.get("kind", "group")
        url = s.get("url")
        role = s.get("role", "community")
        auth_profile = s.get("auth_profile", "default")
        src_enabled = s.get("enabled", True)
        scan_times = s.get("scan_times", ["08:00", "12:00", "16:00", "19:30"])
        tz = s.get("timezone", "UTC")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"facebook.sources[{idx}].name must be a non-empty string")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"facebook.sources[{idx}].url must be a non-empty string")
        if kind not in ("group", "page", "user"):
            raise ValueError(
                f"facebook.sources[{idx}].kind must be 'group', 'page', or 'user', got {kind!r}"
            )
        if role not in ("official", "local_media", "community", "individual", "other"):
            raise ValueError(
                f"facebook.sources[{idx}].role must be one of official, local_media, community, individual, other"
            )
        if not isinstance(scan_times, list) or not all(
            isinstance(t, str) and re.match(r"^\d{2}:\d{2}$", t) for t in scan_times
        ):
            raise ValueError(
                f"facebook.sources[{idx}].scan_times must be a list of 'HH:MM' strings"
            )

        sources.append(
            FacebookSourceBootstrap(
                name=name.strip(),
                kind=kind,
                url=url.strip(),
                role=role,
                auth_profile=auth_profile.strip(),
                enabled=src_enabled,
                scan_times=scan_times,
                timezone=tz,
            )
        )

    return FacebookConfig(
        enabled=enabled,
        editorial_enabled=editorial_enabled,
        auth_root=auth_root.strip(),
        auth_profiles=auth_profiles,
        sources=sources,
        comments=comments_config,
    )
