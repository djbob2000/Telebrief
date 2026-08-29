"""Tests for the SQL-first migration runner and schema compatibility gate."""

from __future__ import annotations

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
    assert version >= 7
    again = await migrate(pg_conn, MIGRATIONS_DIR)
    assert again == version


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
