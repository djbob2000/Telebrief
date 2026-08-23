"""Tests for diagnostic artifact storage, redactions, and retention cleanup (Plan 5 Task 6)."""

import datetime as dt
from pathlib import Path

import psycopg
import pytest

from src.providers.facebook.diagnostics import (
    CollectorArtifactStore,
    redact_sensitive_diagnostics,
)
from src.repositories.facebook import FacebookRepository
from src.retention import RetentionService


class TestDiagnosticsSanitization:
    """Tests for credential and session redaction in diagnostic artifacts."""

    def test_redact_sensitive_diagnostics(self):
        html = '<input name="c_user" value="123456" /> <div data-token="c_user=123456; xs=abcd1234efgh; datr=xyz987">'
        redacted = redact_sensitive_diagnostics(html)
        assert "123456" not in redacted
        assert 'value="[REDACTED]"' in redacted
        assert "c_user=[REDACTED]" in redacted
        assert "xs=[REDACTED]" in redacted
        assert "datr=[REDACTED]" in redacted


@pytest.mark.postgres
class TestCollectorArtifactStoreAndRetention:
    """Integration tests for artifact recording and retention cleanup."""

    async def test_record_creates_sanitized_file_and_metadata(
        self, conn: psycopg.AsyncConnection, edition, tmp_path: Path
    ):
        fb_repo = FacebookRepository()
        store = CollectorArtifactStore(artifacts_root=str(tmp_path / "artifacts"), fb_repo=fb_repo)

        # 1. Insert a source
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'group', 'https://facebook.com/groups/diag', 'https://facebook.com/groups/diag', 'Diag Group', 'community', true)
            RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]

        # 2. Record artifact with sensitive metadata and text
        artifact = await store.record(
            conn,
            source_id=source_id,
            artifact_type="dom_snapshot",
            content="<html><body>Error page: password=secret_pw</body></html>",
            ttl_days=7,
            metadata={"session_id": "sensitive_session", "debug_reason": "unknown_layout"},
        )
        await conn.commit()

        assert artifact.id is not None
        assert artifact.artifact_type == "dom_snapshot"
        assert artifact.metadata["session_id"] == "[REDACTED]"
        assert artifact.metadata["debug_reason"] == "unknown_layout"

        file_path = Path(artifact.storage_path)
        assert file_path.exists()
        saved_text = file_path.read_text(encoding="utf-8")
        assert "secret_pw" not in saved_text
        assert "password=[REDACTED]" in saved_text

    async def test_retention_cleans_only_expired_artifacts(
        self, conn: psycopg.AsyncConnection, uow, edition, tmp_path: Path
    ):
        fb_repo = FacebookRepository()
        store = CollectorArtifactStore(artifacts_root=str(tmp_path / "artifacts"), fb_repo=fb_repo)
        service = RetentionService(uow=uow, fb_repo=fb_repo)

        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'group', 'https://facebook.com/groups/ret_test', 'https://facebook.com/groups/ret_test', 'Retention Group', 'community', true)
            RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]

        # Record 1 expired artifact (ttl = -1 days) and 1 unexpired artifact (ttl = +7 days)
        exp_artifact = await store.record(
            conn,
            source_id=source_id,
            artifact_type="dom_snapshot",
            content="expired",
            ttl_days=-1,
        )
        active_artifact = await store.record(
            conn,
            source_id=source_id,
            artifact_type="screenshot",
            content=b"active_image_bytes",
            ttl_days=7,
        )

        # Also insert a domain SourceItem to ensure retention NEVER touches domain rows
        now = dt.datetime.now(dt.timezone.utc)
        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, published_at, first_collected_at)
            VALUES (%s, 'facebook_post', 'post:keep_me', %s, %s)
            RETURNING id
            """,
            (source_id, now, now),
        )
        post_item_id = (await cur.fetchone())[0]
        await conn.commit()

        # Run retention cleanup
        result = await service.cleanup(now=now)

        assert result.artifacts_deleted == 1
        assert result.files_deleted == 1

        # Expired file deleted, active file preserved
        assert not Path(exp_artifact.storage_path).exists()
        assert Path(active_artifact.storage_path).exists()

        # Domain source item still intact
        cur = await conn.execute("SELECT id FROM source_items WHERE id = %s", (post_item_id,))
        assert await cur.fetchone() is not None
