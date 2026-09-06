"""Facebook provider configuration schemas."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FacebookCommentsConfig:
    include_replies: bool = True
    max_comments_per_post: int = 500
    max_replies_per_comment: int = 100
    max_pages_per_refresh: int = 20
    max_duration_per_post_seconds: int = 120


@dataclass
class FacebookAuthProfileBootstrap:
    name: str
    storage_ref: str


@dataclass
class FacebookSourceBootstrap:
    name: str
    kind: str
    url: str
    role: str = "community"
    auth_profile: str = "default"
    enabled: bool = True
    scan_times: list[str] = field(default_factory=lambda: ["08:00", "12:00", "16:00", "19:30"])
    timezone: str = "UTC"


@dataclass
class FacebookConfig:
    enabled: bool = False
    editorial_enabled: bool = True
    auth_root: str = "/var/lib/telebrief/auth"
    auth_profiles: list[FacebookAuthProfileBootstrap] = field(default_factory=list)
    sources: list[FacebookSourceBootstrap] = field(default_factory=list)
    comments: FacebookCommentsConfig = field(default_factory=FacebookCommentsConfig)
