"""Short-lived collector diagnostics storage and sanitization (Plan 5 Task 6)."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import psycopg

from src.providers.facebook.models import CollectorArtifact
from src.repositories.facebook import FacebookRepository

logger = logging.getLogger(__name__)

# Patterns that must never be recorded in diagnostic artifacts
FORBIDDEN_CREDENTIAL_KEYS = {
    "cookie",
    "cookies",
    "set-cookie",
    "authorization",
    "password",
    "token",
    "access_token",
    "session_id",
    "local_storage",
    "session_storage",
    "c_user",
    "xs",
}

_SENSITIVE_KEY_REGEX = re.compile(
    r"(?i)(c_user|xs|sb|datr|session_id|access_token|password)[\s:=]+[\"']?([^;\s&\"'>]+)"
)
_HTML_INPUT_REGEX = re.compile(
    r'(?i)(name=[\'"]?(c_user|xs|sb|datr|session_id|access_token|password)[\'"]?\s+value=[\'"])([^\'"]+)([\'"])'
)


def redact_sensitive_diagnostics(content: str) -> str:
    """Redact Facebook session cookies, tokens and credentials from HTML/text."""
    # Redact name="c_user" value="..."
    content = _HTML_INPUT_REGEX.sub(r"\1[REDACTED]\4", content)
    # Redact key=value or key: "value"
    return _SENSITIVE_KEY_REGEX.sub(r"\1=[REDACTED]", content)


class CollectorArtifactStore:
    """Stores diagnostic failure snapshots on disk and metadata in the database."""

    def __init__(
        self,
        artifacts_root: str = "data/collector_artifacts",
        fb_repo: FacebookRepository | None = None,
    ) -> None:
        self.artifacts_root = Path(artifacts_root)
        self.fb_repo = fb_repo or FacebookRepository()

    async def record(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        artifact_type: str,
        content: bytes | str,
        ttl_days: int = 14,
        metadata: dict[str, Any] | None = None,
    ) -> CollectorArtifact:
        """Store a diagnostic artifact on disk and register its metadata with TTL."""
        # Sanitize metadata
        clean_meta = dict(metadata or {})
        for key in list(clean_meta.keys()):
            if key.lower() in FORBIDDEN_CREDENTIAL_KEYS:
                clean_meta[key] = "[REDACTED]"

        # Sanitize text content if string
        if isinstance(content, str):
            sanitized_text = redact_sensitive_diagnostics(content)
            raw_bytes = sanitized_text.encode("utf-8")
            extension = "html" if "html" in artifact_type else "txt"
        else:
            raw_bytes = content
            extension = "png" if "screenshot" in artifact_type else "bin"

        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        # Ensure owner-only directory permissions
        try:
            os.chmod(self.artifacts_root, 0o700)
        except OSError:
            pass

        now = dt.datetime.now(dt.timezone.utc)
        content_hash = hashlib.sha256(raw_bytes).hexdigest()[:12]
        filename = f"{source_id}_{artifact_type}_{int(now.timestamp())}_{content_hash}.{extension}"
        target_path = self.artifacts_root / filename

        target_path.write_bytes(raw_bytes)
        try:
            target_path.chmod(0o600)
        except OSError:
            pass

        expires_at = now + dt.timedelta(days=ttl_days)
        return await self.fb_repo.insert_artifact(
            conn,
            source_id=source_id,
            artifact_type=artifact_type,
            storage_path=str(target_path),
            expires_at=expires_at,
            metadata=clean_meta,
        )
