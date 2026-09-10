"""Tests for the SQL-first migration runner and schema compatibility gate."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import psycopg
import pytest

from src.db.migrations import migrate
from src.db.schema_version import SchemaVersionError, require_schema_compatible

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Probe versions use a high range so they can never collide with real
# migration versions recorded in a shared ledger.
PROBE_FIRST = 900003
PROBE_SECOND = 900004
PROBE_DUP_A = 900010
PROBE_OK = 900020
PROBE_BROKEN = 900021
PROBE_NT_TABLE = 900030
PROBE_NT_REBUILD = 900031


@pytest.mark.postgres
async def test_migrate_applies_each_version_once(pg_conn):
    version = await migrate(pg_conn, MIGRATIONS_DIR)
    assert version >= 33
    again = await migrate(pg_conn, MIGRATIONS_DIR)
    assert again == version


@pytest.mark.postgres
async def test_publication_refresh_has_frozen_knowledge_snapshot(pg_conn):
    await migrate(pg_conn, MIGRATIONS_DIR)
    cursor = await pg_conn.execute(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'publication_refresh_runs'
          AND column_name = 'knowledge_snapshot_at'
        """
    )
    assert await cursor.fetchone() == ("timestamp with time zone", "YES")


@pytest.mark.postgres
async def test_require_schema_compatible_returns_current_version(pg_conn):
    from src.bootstrap import SCHEMA_VERSION_MAXIMUM

    current = await require_schema_compatible(pg_conn, minimum=7, maximum=SCHEMA_VERSION_MAXIMUM)
    assert current >= 7


@pytest.mark.postgres
async def test_schema_compatibility_rejects_old_database(pg_conn):
    await pg_conn.execute("TRUNCATE telebrief_schema_migrations")
    with pytest.raises(SchemaVersionError):
        await require_schema_compatible(pg_conn, minimum=3, maximum=3)


@pytest.mark.postgres
async def test_schema_compatibility_rejects_newer_database(pg_conn):
    with pytest.raises(SchemaVersionError):
        await require_schema_compatible(pg_conn, minimum=1, maximum=2)


@pytest.mark.postgres
async def test_migrate_records_versions_in_order(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_SECOND:06d}_second.sql").write_text(
        f"CREATE TABLE order_probe_{PROBE_SECOND} (id integer);\n"
        f"SELECT * FROM order_probe_{PROBE_FIRST};\n",
        encoding="utf-8",
    )
    (tmp_path / f"{PROBE_FIRST:06d}_first.sql").write_text(
        f"CREATE TABLE order_probe_{PROBE_FIRST} (id integer);\n",
        encoding="utf-8",
    )

    version = await migrate(isolated_pg_conn, tmp_path)

    assert version == PROBE_SECOND
    cursor = await isolated_pg_conn.execute(
        "SELECT version FROM telebrief_schema_migrations ORDER BY version"
    )
    rows = await cursor.fetchall()
    assert [row[0] for row in rows] == [PROBE_FIRST, PROBE_SECOND]


@pytest.mark.postgres
async def test_migrate_rejects_duplicate_versions(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_DUP_A:06d}_alpha.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / f"{PROBE_DUP_A:06d}_beta.sql").write_text("SELECT 2;\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        await migrate(isolated_pg_conn, tmp_path)


@pytest.mark.postgres
async def test_failed_migration_is_not_recorded(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_OK:06d}_create_probe.sql").write_text(
        "CREATE TABLE rollback_probe_ok (id integer);\n", encoding="utf-8"
    )
    (tmp_path / f"{PROBE_BROKEN:06d}_broken.sql").write_text(
        "THIS IS NOT VALID SQL;\n", encoding="utf-8"
    )

    with pytest.raises(psycopg.errors.SyntaxError):
        await migrate(isolated_pg_conn, tmp_path)

    cursor = await isolated_pg_conn.execute("SELECT to_regclass('rollback_probe_ok')")
    assert (await cursor.fetchone())[0] is not None
    cursor = await isolated_pg_conn.execute(
        "SELECT version FROM telebrief_schema_migrations ORDER BY version"
    )
    rows = await cursor.fetchall()
    assert [row[0] for row in rows] == [PROBE_OK]


@pytest.mark.postgres
async def test_non_transactional_migration_header(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_NT_TABLE:06d}_table.sql").write_text(
        "CREATE TABLE nontransactional_probe (id integer);\n", encoding="utf-8"
    )
    (tmp_path / f"{PROBE_NT_REBUILD:06d}_rebuild.sql").write_text(
        "-- telebrief: non-transactional\n"
        "DROP TABLE IF EXISTS nontransactional_probe;\n"
        "CREATE TABLE nontransactional_probe (id integer);\n",
        encoding="utf-8",
    )

    version = await migrate(isolated_pg_conn, tmp_path)

    assert version == PROBE_NT_REBUILD
    cursor = await isolated_pg_conn.execute("SELECT count(*) FROM nontransactional_probe")
    assert (await cursor.fetchone())[0] == 0
    cursor = await isolated_pg_conn.execute(
        "SELECT version FROM telebrief_schema_migrations ORDER BY version"
    )
    rows = await cursor.fetchall()
    assert [row[0] for row in rows] == [PROBE_NT_TABLE, PROBE_NT_REBUILD]


@pytest.mark.postgres
async def test_publication_refresh_readiness_schema(pg_conn):
    await migrate(pg_conn, MIGRATIONS_DIR)

    cur = await pg_conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'publication_runs'
        """
    )
    assert "source_cutoff_at" in {row[0] for row in await cur.fetchall()}

    cur = await pg_conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ('publication_refresh_runs', 'publication_refresh_sources')
        ORDER BY table_name
        """
    )
    assert [row[0] for row in await cur.fetchall()] == [
        "publication_refresh_runs",
        "publication_refresh_sources",
    ]

    cur = await pg_conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'publication_refresh_runs'
        """
    )
    assert "lookback_hours" in {row[0] for row in await cur.fetchall()}

    cur = await pg_conn.execute(
        """
        SELECT column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'publication_refresh_runs'
          AND column_name = 'lookback_hours'
        """
    )
    assert (await cur.fetchone())[0] == "24"

    cur = await pg_conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'collection_run_revision_observations'
        """
    )
    assert await cur.fetchone() == ("collection_run_revision_observations",)


@pytest.mark.postgres
async def test_publication_lookback_repair_only_updates_legacy_open_rows(
    isolated_pg_conn, tmp_path
):
    """0032 repairs rows from before 0031 without clobbering later overrides."""
    for migration in MIGRATIONS_DIR.glob("*.sql"):
        if migration.name.startswith("0032_"):
            continue
        shutil.copy2(migration, tmp_path / migration.name)

    from src.db.migrations import migrate

    await migrate(isolated_pg_conn, tmp_path)
    boundary = dt.datetime(2026, 9, 10, 8, 0, tzinfo=dt.timezone.utc)
    cursor = await isolated_pg_conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('lookback-repair', 'Lookback Repair') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    async def insert_run(
        publication_type: str,
        status: str,
        created_at: dt.datetime,
        request_key: str,
        slot_at: dt.datetime,
    ) -> int:
        cursor = await isolated_pg_conn.execute(
            """
            INSERT INTO publication_refresh_runs (
                edition_id, publication_type, slot_at, requested_at,
                normal_source_cutoff_at, fallback_snapshot_at, deadline_at,
                status, trigger, request_key, freshness_cutoff_at,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s, %s, %s, %s)
            RETURNING id
            """,
            (
                edition_id,
                publication_type,
                slot_at,
                boundary,
                boundary,
                boundary,
                boundary + dt.timedelta(hours=1),
                status,
                request_key,
                boundary,
                created_at,
                created_at,
            ),
        )
        return int((await cursor.fetchone())[0])

    weekly_legacy = await insert_run(
        "weekly_article",
        "collecting",
        boundary - dt.timedelta(hours=1),
        "repair:weekly",
        boundary,
    )
    monthly_legacy = await insert_run(
        "monthly_article",
        "preparing",
        boundary - dt.timedelta(hours=1),
        "repair:monthly",
        boundary,
    )
    terminal_legacy = await insert_run(
        "weekly_article",
        "failed",
        boundary - dt.timedelta(hours=1),
        "repair:terminal",
        boundary + dt.timedelta(hours=1),
    )
    queued_legacy = await insert_run(
        "monthly_article",
        "publication_queued",
        boundary - dt.timedelta(hours=1),
        "repair:queued",
        boundary + dt.timedelta(hours=1),
    )
    post_migration_override = await insert_run(
        "weekly_article",
        "collecting",
        boundary + dt.timedelta(seconds=1),
        "repair:override",
        boundary + dt.timedelta(hours=2),
    )

    await isolated_pg_conn.execute(
        "UPDATE telebrief_schema_migrations SET applied_at = %s WHERE version = 31",
        (boundary,),
    )
    assert await migrate(isolated_pg_conn, MIGRATIONS_DIR) == 32

    cursor = await isolated_pg_conn.execute(
        "SELECT id, lookback_hours FROM publication_refresh_runs WHERE id = ANY(%s)",
        ([weekly_legacy, monthly_legacy, terminal_legacy, queued_legacy, post_migration_override],),
    )
    values = {int(row[0]): int(row[1]) for row in await cursor.fetchall()}
    assert values == {
        weekly_legacy: 168,
        monthly_legacy: 720,
        terminal_legacy: 24,
        queued_legacy: 24,
        post_migration_override: 24,
    }


@pytest.mark.postgres
async def test_event_edition_scope_schema(pg_conn):
    await migrate(pg_conn, MIGRATIONS_DIR)
    cur = await pg_conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'story_edition_scope_decisions'
        ORDER BY ordinal_position
        """
    )
    columns = [row[0] for row in await cur.fetchall()]
    assert "scope_class" in columns
    assert "scope_config_hash" in columns
    assert "latest_assignment_id" in columns


@pytest.mark.postgres
async def test_event_gate_enrichment_schema(pg_conn):
    await migrate(pg_conn, MIGRATIONS_DIR)
    cur = await pg_conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'story_event_triage_decisions'
        ORDER BY ordinal_position
        """
    )
    columns = [row[0] for row in await cur.fetchall()]
    assert "scope_config_hash" in columns
    assert "retention" in columns
    assert "enrichment" in columns
    assert "brief_payload" in columns

    # Test constraints on story_event_triage_decisions
    # We test invalid retention/enrichment combinations by attempting inserts
    import uuid

    uid = uuid.uuid4().hex[:8]
    cur = await pg_conn.execute("SELECT id FROM editions LIMIT 1")
    edition_row = await cur.fetchone()
    if not edition_row:
        cur = await pg_conn.execute(
            f"INSERT INTO editions (slug, name) VALUES ('test_ed_{uid}', 'Test') RETURNING id"
        )
        edition_id = (await cur.fetchone())[0]
    else:
        edition_id = edition_row[0]

    cur = await pg_conn.execute(
        "INSERT INTO stories (edition_id, knowledge_source) VALUES (%s, 'event_first') RETURNING id",
        (edition_id,),
    )
    story_id = (await cur.fetchone())[0]

    cur = await pg_conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', %s, 'https://t.me/mig', 'Mig')
        RETURNING id
        """,
        (f"ext-mig-{uid}",),
    )
    source_id = (await cur.fetchone())[0]
    cur = await pg_conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', %s, now()) RETURNING id",
        (source_id, f"item-{uid}"),
    )
    item_id = (await cur.fetchone())[0]
    cur = await pg_conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, %s, 'txt') RETURNING id",
        (item_id, f"h-mig-{uid}"),
    )
    rev_id = (await cur.fetchone())[0]
    cur = await pg_conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate)
        VALUES (%s, 1, 'frag text', %s, 'v1', true)
        RETURNING id
        """,
        (rev_id, f"h-frag-{uid}"),
    )
    frag_id = (await cur.fetchone())[0]
    cur = await pg_conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
        VALUES (%s, '[0.1, 0.2]'::vector, 'm', 2)
        RETURNING id
        """,
        (f"h-frag-{uid}",),
    )
    vec_id = (await cur.fetchone())[0]
    cur = await pg_conn.execute(
        "INSERT INTO source_fragment_embeddings (fragment_id, vector_id) VALUES (%s, %s) RETURNING id",
        (frag_id, vec_id),
    )
    emb_id = (await cur.fetchone())[0]
    cur = await pg_conn.execute(
        "INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind) VALUES (%s, %s, %s, 'new_story') RETURNING id",
        (story_id, frag_id, emb_id),
    )
    assign_id = (await cur.fetchone())[0]

    cur = await pg_conn.execute(
        """
        INSERT INTO story_event_triage_runs (triage_version, provider, model, prompt_hash, story_count, input_chars, status)
        VALUES ('v2', 'openai', 'gpt', 'phash', 1, 100, 'succeeded')
        RETURNING id
        """
    )
    run_id = (await cur.fetchone())[0]

    # Test valid combinations
    # DROP + NONE: valid
    await pg_conn.execute(
        """
        INSERT INTO story_event_triage_decisions (
            run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
            decision, retention, enrichment, confidence, reason
        ) VALUES (%s, %s, %s, 'v2', 'hash1', 'IGNORE', 'DROP', 'NONE', 1.0, 'noise')
        """,
        (run_id, story_id, assign_id),
    )

    # DROP + BRIEF: invalid
    with pytest.raises(psycopg.Error):
        await pg_conn.execute(
            """
            INSERT INTO story_event_triage_decisions (
                run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
                decision, retention, enrichment, confidence, reason
            ) VALUES (%s, %s, %s, 'v2', 'hash2', 'IGNORE', 'DROP', 'BRIEF', 1.0, 'invalid')
            """,
            (run_id, story_id, assign_id),
        )

    # KEEP + NONE: invalid
    with pytest.raises(psycopg.Error):
        await pg_conn.execute(
            """
            INSERT INTO story_event_triage_decisions (
                run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
                decision, retention, enrichment, confidence, reason
            ) VALUES (%s, %s, %s, 'v2', 'hash3', 'ANALYZE', 'KEEP', 'NONE', 1.0, 'invalid')
            """,
            (run_id, story_id, assign_id),
        )

    # KEEP + BRIEF: valid
    await pg_conn.execute(
        """
        INSERT INTO story_event_triage_decisions (
            run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
            decision, retention, enrichment, confidence, reason
        ) VALUES (%s, %s, %s, 'v2', 'hash4', 'ANALYZE', 'KEEP', 'BRIEF', 1.0, 'valid')
        """,
        (run_id, story_id, assign_id),
    )

    # KEEP + ANALYZE: valid
    await pg_conn.execute(
        """
        INSERT INTO story_event_triage_decisions (
            run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
            decision, retention, enrichment, confidence, reason
        ) VALUES (%s, %s, %s, 'v2', 'hash5', 'ANALYZE', 'KEEP', 'ANALYZE', 1.0, 'valid')
        """,
        (run_id, story_id, assign_id),
    )


@pytest.mark.postgres
async def test_event_processing_execution_guard_schema(pg_conn):
    await migrate(pg_conn, MIGRATIONS_DIR)

    cur = await pg_conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name IN (
              'event_processing_cycle_leases',
              'story_event_processing_claims',
              'event_revision_processing_state'
          )
        ORDER BY table_name
        """
    )
    assert [row[0] for row in await cur.fetchall()] == [
        "event_processing_cycle_leases",
        "event_revision_processing_state",
        "story_event_processing_claims",
    ]

    cur = await pg_conn.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND (
              (table_name = 'source_item_revisions' AND column_name = 'collection_run_id')
              OR (table_name = 'story_revisions' AND column_name = 'event_assignment_id')
          )
        ORDER BY table_name, column_name
        """
    )
    assert await cur.fetchall() == [
        ("source_item_revisions", "collection_run_id"),
        ("story_revisions", "event_assignment_id"),
    ]


@pytest.mark.postgres
async def test_event_processing_semantic_reuse_schema(pg_conn):
    await migrate(pg_conn, MIGRATIONS_DIR)

    cur = await pg_conn.execute(
        """
        SELECT table_name, column_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (
              (table_name = 'source_item_revisions' AND column_name IN ('event_processing_hash', 'event_input_version'))
              OR (table_name = 'event_revision_processing_state' AND column_name IN ('processing_mode', 'reused_from_revision_id'))
          )
        ORDER BY table_name, column_name
        """
    )
    assert await cur.fetchall() == [
        ("event_revision_processing_state", "processing_mode", "NO"),
        ("event_revision_processing_state", "reused_from_revision_id", "YES"),
        ("source_item_revisions", "event_input_version", "YES"),
        ("source_item_revisions", "event_processing_hash", "YES"),
    ]

    cur = await pg_conn.execute(
        """
        SELECT constraint_name
        FROM information_schema.check_constraints
        WHERE constraint_schema = 'public'
          AND constraint_name = 'event_revision_processing_state_processing_mode_check'
        """
    )
    assert await cur.fetchone() is not None


@pytest.mark.postgres
async def test_unified_publication_intent_schema(pg_conn):
    await migrate(pg_conn, MIGRATIONS_DIR)

    cur = await pg_conn.execute(
        """
        SELECT column_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'publication_refresh_runs'
          AND column_name IN ('trigger', 'request_key', 'freshness_cutoff_at', 'requested_by_user_id')
        ORDER BY column_name
        """
    )
    assert await cur.fetchall() == [
        ("freshness_cutoff_at", "NO"),
        ("request_key", "NO"),
        ("requested_by_user_id", "YES"),
        ("trigger", "NO"),
    ]

    cur = await pg_conn.execute(
        """
        SELECT constraint_name
        FROM information_schema.table_constraints
        WHERE table_schema = 'public'
          AND table_name = 'publication_refresh_runs'
          AND constraint_type = 'UNIQUE'
        """
    )
    assert await cur.fetchone() is not None

    cur = await pg_conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'publication_failure_notifications'
        """
    )
    assert await cur.fetchone() == ("publication_failure_notifications",)

    cur = await pg_conn.execute(
        """
        SELECT check_clause
        FROM information_schema.check_constraints
        WHERE constraint_schema = 'public'
          AND constraint_name = 'publication_refresh_runs_status_check'
        """
    )
    row = await cur.fetchone()
    assert row is not None
    assert "fallback_ready" not in row[0]

    cur = await pg_conn.execute(
        """
        SELECT 1
        FROM information_schema.referential_constraints
        WHERE constraint_schema = 'public'
          AND constraint_name IN (
              SELECT constraint_name
              FROM information_schema.key_column_usage
              WHERE table_schema = 'public'
                AND table_name = 'event_revision_processing_state'
                AND column_name = 'reused_from_revision_id'
          )
        """
    )
    assert await cur.fetchone() is not None
