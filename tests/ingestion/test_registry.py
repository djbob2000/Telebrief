"""Bootstrap behaviour: config.yaml becomes idempotent source registry rows.

Task 4 pins the SourceRegistry.bootstrap_from_config contract against the
persistent test database:

* running bootstrap twice never duplicates sources;
* the default `berdyansk` edition exists and every bootstrap-managed enabled
  Telegram source is bound to it through source_editions;
* legacy editorial roles map explicitly (news -> local_media,
  community -> community, official -> official, classifieds -> other,
  mixed -> other) with unknown values falling back to `other`;
* collector_options defaults mirror config.collection.telegram_interval_minutes
  (default 45) and follow later config changes for bootstrap-managed rows;
* rows switched to management_mode='database' are never touched again.

Tests prefer the real loader path (load_config against a tmp YAML snippet)
so parsing is exercised; the unknown-role fallback uses a minimal fake config
object because load_config itself rejects unknown source_type values.
"""

from __future__ import annotations

import pytest

from src.config_loader import CollectionConfig, load_config
from src.ingestion.registry import BootstrapResult, SourceRegistry

TEST_YAML_WITH_ROLES = """
channels:
  - id: "@news_city"
    name: "City News"
    source_type: news
  - id: "@community_board"
    name: "Community Board"
    source_type: community
  - id: "@city_official"
    name: "City Official"
    source_type: official
  - id: "@classifieds"
    name: "Classifieds"
    source_type: classifieds
  - id: "@general"
    name: "General"
    source_type: mixed
  - id: -1001234567890
    name: "Private Group"

settings:
  target_user_id: 123456789
"""

TEST_YAML_INTERVAL_30 = """
channels:
  - id: "@news_city"
    name: "City News"
    source_type: news

settings:
  target_user_id: 123456789

collection:
  telegram_interval_minutes: 30
"""

TEST_YAML_INTERVAL_60 = """
channels:
  - id: "@news_city"
    name: "City News"
    source_type: news

settings:
  target_user_id: 123456789

collection:
  telegram_interval_minutes: 60
"""


@pytest.fixture
def config_with_roles(tmp_path, mock_env_vars):
    """Real loader path: six-channel tmp config without a collection block."""
    path = tmp_path / "config.yaml"
    path.write_text(TEST_YAML_WITH_ROLES)
    return load_config(str(path))


async def _fetch_sources(conn) -> dict[str, tuple]:
    cursor = await conn.execute(
        """
        SELECT external_id, name, role, enabled, url, collector_options,
               management_mode
        FROM sources ORDER BY external_id
        """
    )
    return {row[0]: row for row in await cursor.fetchall()}


@pytest.mark.postgres
async def test_bootstrap_twice_is_idempotent(conn, config_with_roles):
    """A second bootstrap run must not duplicate sources or bindings."""
    registry = SourceRegistry()

    first = await registry.bootstrap_from_config(conn, config_with_roles)
    assert isinstance(first, BootstrapResult)
    assert first.sources_created == 6
    assert first.sources_updated == 0
    assert first.sources_unchanged == 0
    assert first.sources_skipped_db_managed == 0
    assert first.bindings_created == 6
    assert first.edition_created is True

    rows_after_first = await _fetch_sources(conn)

    second = await registry.bootstrap_from_config(conn, config_with_roles)
    assert second.sources_created == 0
    assert second.sources_updated == 0
    assert second.sources_unchanged == 6
    assert second.sources_skipped_db_managed == 0
    assert second.bindings_created == 0
    assert second.edition_created is False

    rows_after_second = await _fetch_sources(conn)
    assert rows_after_second.keys() == rows_after_first.keys()
    assert rows_after_second["@news_city"] == rows_after_first["@news_city"]


@pytest.mark.postgres
async def test_berdyansk_edition_created_with_defaults(conn, config_with_roles):
    """Bootstrap ensures the berdyansk edition with sensible defaults."""
    await SourceRegistry().bootstrap_from_config(conn, config_with_roles)

    cursor = await conn.execute(
        """
        SELECT slug, name, timezone, language, enabled
        FROM editions WHERE slug = 'berdyansk'
        """
    )
    row = await cursor.fetchone()
    assert row is not None
    slug, name, timezone_, language, enabled = row
    assert slug == "berdyansk"
    assert name == "Бердянск"
    assert timezone_ == "Europe/Kyiv"
    assert language == "ru"
    assert enabled is True


@pytest.mark.postgres
async def test_role_mappings_and_edition_bindings(conn, config_with_roles):
    """Legacy roles map exactly; every enabled configured channel is bound."""
    expected_roles = {
        "@news_city": "local_media",
        "@community_board": "community",
        "@city_official": "official",
        "@classifieds": "other",
        "@general": "other",
        "-1001234567890": "other",
    }

    await SourceRegistry().bootstrap_from_config(conn, config_with_roles)

    cursor = await conn.execute(
        """
        SELECT s.external_id, s.role, se.edition_id IS NOT NULL AS bound
        FROM sources s
        LEFT JOIN source_editions se ON se.source_id = s.id
        ORDER BY s.external_id
        """
    )
    rows = await cursor.fetchall()
    assert {r[0] for r in rows} == set(expected_roles)
    for external_id, role, bound in rows:
        assert role == expected_roles[external_id], external_id
        assert bound is True, external_id


@pytest.mark.postgres
async def test_url_and_external_id_rules(conn, config_with_roles):
    """@-handles keep their written form and gain a t.me URL; numeric ids don't."""
    await SourceRegistry().bootstrap_from_config(conn, config_with_roles)

    cursor = await conn.execute(
        """
        SELECT external_id, url FROM sources
        WHERE external_id IN ('@news_city', '-1001234567890')
        ORDER BY external_id
        """
    )
    by_external_id = {row[0]: row[1] for row in await cursor.fetchall()}
    assert by_external_id["@news_city"] == "https://t.me/news_city"
    assert by_external_id["-1001234567890"] is None


@pytest.mark.postgres
async def test_default_collector_options_interval(conn, config_with_roles):
    """Without a collection block every managed source gets interval 45."""
    await SourceRegistry().bootstrap_from_config(conn, config_with_roles)

    cursor = await conn.execute(
        "SELECT external_id, collector_options FROM sources ORDER BY external_id"
    )
    for external_id, options in await cursor.fetchall():
        assert options == {"schedule": {"interval_minutes": 45}}, external_id


@pytest.mark.postgres
async def test_collection_interval_parsed_and_copied(tmp_path, mock_env_vars, conn):
    """A configured interval lands in collector_options and follows changes."""
    path = tmp_path / "config.yaml"
    path.write_text(TEST_YAML_INTERVAL_30)
    config = load_config(str(path))
    assert config.collection.telegram_interval_minutes == 30

    registry = SourceRegistry()
    await registry.bootstrap_from_config(conn, config)

    cursor = await conn.execute(
        "SELECT collector_options FROM sources WHERE external_id = '@news_city'"
    )
    (options,) = await cursor.fetchone()
    assert options == {"schedule": {"interval_minutes": 30}}

    path.write_text(TEST_YAML_INTERVAL_60)
    updated_result = await registry.bootstrap_from_config(conn, load_config(str(path)))
    assert updated_result.sources_updated == 1

    cursor = await conn.execute(
        "SELECT collector_options FROM sources WHERE external_id = '@news_city'"
    )
    (options,) = await cursor.fetchone()
    assert options == {"schedule": {"interval_minutes": 60}}


@pytest.mark.parametrize("invalid", ["4", "361", "0", "true", "12.5", "x"])
def test_collection_interval_validation(tmp_path, mock_env_vars, invalid):
    """telegram_interval_minutes must be an int in [5, 360]."""
    yaml_text = TEST_YAML_INTERVAL_30.replace(
        "telegram_interval_minutes: 30", f"telegram_interval_minutes: {invalid}"
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text)
    with pytest.raises(ValueError, match="telegram_interval_minutes"):
        load_config(str(path))


def test_collection_defaults_when_block_absent(tmp_path, mock_env_vars):
    """No collection block means CollectionConfig() with the default of 45."""
    path = tmp_path / "config.yaml"
    path.write_text(TEST_YAML_WITH_ROLES)
    config = load_config(str(path))
    assert isinstance(config.collection, CollectionConfig)
    assert config.collection.telegram_interval_minutes == 45


@pytest.mark.parametrize("boundary", [5, 360])
def test_collection_interval_boundaries_accepted(tmp_path, mock_env_vars, boundary):
    yaml_text = TEST_YAML_INTERVAL_30.replace(
        "telegram_interval_minutes: 30", f"telegram_interval_minutes: {boundary}"
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text)
    config = load_config(str(path))
    assert config.collection.telegram_interval_minutes == boundary


@pytest.mark.postgres
async def test_unknown_source_type_falls_back_to_other(conn):
    """Unknown configured roles degrade to 'other' instead of crashing.

    Uses a minimal fake config because load_config rejects unknown
    source_type values at parse time.
    """
    from types import SimpleNamespace

    fake_channel = SimpleNamespace(id="@weird_channel", name="Weird", source_type="alien")
    fake_config = SimpleNamespace(channels=[fake_channel], collection=CollectionConfig())

    result = await SourceRegistry().bootstrap_from_config(conn, fake_config)

    assert result.sources_created == 1
    cursor = await conn.execute(
        "SELECT role, enabled FROM sources WHERE external_id = '@weird_channel'"
    )
    role, enabled = await cursor.fetchone()
    assert role == "other"
    assert enabled is True


@pytest.mark.postgres
async def test_db_managed_source_survives_rebootstrap(conn, config_with_roles):
    """Flipping a source to management_mode='database' freezes bootstrap."""
    registry = SourceRegistry()
    await registry.bootstrap_from_config(conn, config_with_roles)

    await conn.execute(
        """
        UPDATE sources SET management_mode = 'database',
            name = 'Operator Name',
            role = 'individual',
            enabled = false,
            collector_options = '{"schedule": {"interval_minutes": 99}}'::jsonb
        WHERE external_id = '@news_city'
        """
    )

    second = await registry.bootstrap_from_config(conn, config_with_roles)

    assert second.sources_skipped_db_managed == 1
    assert second.sources_updated == 0
    assert second.sources_unchanged == 5
    assert second.sources_created == 0
    assert second.bindings_created == 0

    rows = await _fetch_sources(conn)
    news_row = rows["@news_city"]
    assert news_row[1] == "Operator Name"
    assert news_row[2] == "individual"
    assert news_row[3] is False
    assert news_row[5] == {"schedule": {"interval_minutes": 99}}
    assert news_row[6] == "database"

    cursor = await conn.execute(
        """
        SELECT count(*) FROM source_editions se
        JOIN sources s ON s.id = se.source_id
        WHERE s.external_id = '@news_city'
        """
    )
    (binding_count,) = await cursor.fetchone()
    assert binding_count == 1
