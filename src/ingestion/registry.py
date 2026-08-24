"""Source registry bootstrap: turn legacy config.yaml channels into rows.

`SourceRegistry.bootstrap_from_config` is the one-shot importer that seeds
the provider-neutral `sources` / `editions` / `source_editions` tables from
the Telegram-centric configuration. Design rules:

* **Idempotent.** Every channel maps to exactly one source identity
  ``(platform='telegram', kind='channel', external_id=<id as written>)``;
  re-running bootstrap converges instead of duplicating.
* **Config is bootstrap data, not runtime truth.** Once a row exists the
  database owns it; a later bootstrap only refreshes bootstrap-controlled
  defaults while ``management_mode='bootstrap'``. Rows flipped to
  ``management_mode='database'`` are detected by that column alone (never by
  timestamps) and are skipped entirely — never refreshed, never (re)bound.
* **Roles map exactly** from the editorial ``source_type`` vocabulary:
  news -> local_media, community -> community, official -> official,
  classifieds -> other, mixed -> other. Unknown values degrade to 'other'
  rather than failing bootstrap (load_config normally rejects them anyway).
  Topic-level ``source_type`` overrides are deliberately ignored here: they
  are provider metadata carried later on Telegram observations (Task 5) and
  must never mutate the global Source role.
* **URLs**: ids starting with '@' become ``https://t.me/<id-without-@>``;
  every other id form (numeric private chat ids) gets no URL.
* **Enabled**: bootstrap-managed sources are always created enabled; there
  is no disable flag in the legacy channel config.

Repositories/services take an explicit connection; the registry follows the
same discipline and never commits — the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from src.config_loader import Config
from src.domain.editions import Edition, NewEdition
from src.domain.sources import NewSource, Source
from src.ingestion.protocol import Collector
from src.repositories.editions import EditionRepository

PLATFORM_TELEGRAM = "telegram"
KIND_CHANNEL = "channel"

DEFAULT_EDITION_SLUG = "berdyansk"

# Legacy editorial source_type -> neutral Source role. Exact mapping by design
# (brief): classifieds/mixed collapse into 'other'; unknown values also land
# on 'other' so a bad config value degrades gracefully at bootstrap time.
ROLE_BY_SOURCE_TYPE = {
    "news": "local_media",
    "community": "community",
    "official": "official",
    "classifieds": "other",
    "mixed": "other",
}


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of one :meth:`SourceRegistry.bootstrap_from_config` pass.

    Attributes:
        sources_created: rows INSERTed because their identity was unseen.
        sources_updated: bootstrap-managed rows whose config-derived fields
            (name/url/role/enabled/collector_options) changed.
        sources_unchanged: bootstrap-managed rows already matching config.
        sources_skipped_db_managed: rows with ``management_mode='database'``;
            bootstrap neither read nor wrote their operator-owned values.
        bindings_created: new ``source_editions`` rows (rebinds are no-ops).
        edition_created: True when the default edition had to be created.
    """

    sources_created: int
    sources_updated: int
    sources_unchanged: int
    sources_skipped_db_managed: int
    bindings_created: int
    edition_created: bool


async def upsert_bootstrap_source(
    conn: psycopg.AsyncConnection, source: NewSource
) -> Source | None:
    """Insert-or-refresh one bootstrap-managed source row.

    The UPDATE arm only fires while the existing row is still
    ``management_mode='bootstrap'``; DB-managed rows refuse the update and,
    because PostgreSQL does not RETURNING-skipped rows, yield ``None`` here.
    Returns the resulting Source otherwise.
    """
    cur = await conn.execute(
        """
        INSERT INTO sources(platform, kind, external_id, url, name, role, enabled, collector_options, management_mode)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'bootstrap')
        ON CONFLICT (platform, kind, external_id) WHERE external_id IS NOT NULL
        DO UPDATE SET
            name = EXCLUDED.name,
            url = EXCLUDED.url,
            role = EXCLUDED.role,
            enabled = EXCLUDED.enabled,
            collector_options = EXCLUDED.collector_options
        WHERE sources.management_mode = 'bootstrap'
        RETURNING *
        """,
        (
            source.platform,
            source.kind,
            source.external_id,
            source.url,
            source.name,
            source.role,
            source.enabled,
            Jsonb(source.collector_options),
        ),
    )
    row = await cur.fetchone()
    return None if row is None else Source.from_row(row)


def build_bootstrap_source(channel: Any, interval_minutes: int) -> NewSource:
    """Map one configured Telegram channel onto its source identity.

    The external id keeps the configured spelling ('@handle' stays a handle,
    numeric ids stringify); topic-level role overrides are intentionally not
    consulted — they ride along as observation metadata in Task 5.
    """
    external_id = str(channel.id)
    url = f"https://t.me/{external_id[1:]}" if external_id.startswith("@") else None
    raw_role = getattr(channel, "source_type", None)
    role = ROLE_BY_SOURCE_TYPE.get(raw_role, "other") if isinstance(raw_role, str) else "other"
    return NewSource(
        platform=PLATFORM_TELEGRAM,
        kind=KIND_CHANNEL,
        external_id=external_id,
        url=url,
        name=channel.name,
        role=role,
        enabled=True,
        collector_options={"schedule": {"interval_minutes": interval_minutes}},
    )


async def _fetch_existing(
    conn: psycopg.AsyncConnection,
    platform: str,
    kind: str,
    external_id: str,
) -> SimpleNamespace | None:
    """Read the mutable-by-bootstrap fields plus the ownership mode."""
    cursor = await conn.execute(
        """
        SELECT name, url, role, enabled, collector_options, management_mode
        FROM sources
        WHERE platform = %s AND kind = %s AND external_id = %s
        """,
        (platform, kind, external_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return SimpleNamespace(
        name=row[0],
        url=row[1],
        role=row[2],
        enabled=row[3],
        collector_options=row[4],
        management_mode=row[5],
    )


class SourceRegistry:
    """Seeds and refreshes bootstrap-managed sources from configuration."""

    def __init__(self, editions: EditionRepository | None = None):
        self._editions = editions if editions is not None else EditionRepository()

    async def bootstrap_from_config(
        self, conn: psycopg.AsyncConnection, config: Config
    ) -> BootstrapResult:
        """Upsert every configured Telegram channel and Facebook source and bind them to the edition."""
        created = updated = unchanged = skipped = 0

        edition, edition_created = await self._ensure_edition(conn)

        interval_minutes = config.collection.telegram_interval_minutes
        bindings_created = 0

        for channel in config.channels:
            external_id = str(channel.id)
            existing = await _fetch_existing(conn, PLATFORM_TELEGRAM, KIND_CHANNEL, external_id)
            if existing is not None and existing.management_mode == "database":
                skipped += 1
                continue

            candidate = build_bootstrap_source(channel, interval_minutes)
            source = await upsert_bootstrap_source(conn, candidate)
            if source is None:
                skipped += 1
                continue

            if existing is None:
                created += 1
            elif _differs(existing, source):
                updated += 1
            else:
                unchanged += 1

            bindings_created += await self._bind(conn, source.id, edition.id)

        # Bootstrap Facebook auth profiles and sources
        if (
            getattr(config, "facebook", None) is not None
            and config.facebook.enabled
            and config.facebook.sources
        ):
            from src.repositories.facebook import FacebookRepository

            fb_repo = FacebookRepository()
            for prof in config.facebook.auth_profiles:
                await fb_repo.get_or_create_auth_profile(
                    conn, name=prof.name, storage_ref=prof.storage_ref
                )

            for fb_source in config.facebook.sources:
                external_id = fb_source.url
                existing = await _fetch_existing(conn, "facebook", fb_source.kind, external_id)
                if existing is not None and existing.management_mode == "database":
                    skipped += 1
                    continue

                fb_candidate = NewSource(
                    platform="facebook",
                    kind=fb_source.kind,
                    external_id=external_id,
                    url=fb_source.url,
                    name=fb_source.name,
                    role=fb_source.role,
                    enabled=fb_source.enabled,
                    collector_options={
                        "schedule": {
                            "type": "daily_times",
                            "daily_times": fb_source.scan_times,
                            "timezone": fb_source.timezone,
                        }
                    },
                )
                source = await upsert_bootstrap_source(conn, fb_candidate)
                if source is None:
                    skipped += 1
                    continue

                auth_profile = await fb_repo.get_or_create_auth_profile(
                    conn, name=fb_source.auth_profile, storage_ref=fb_source.auth_profile
                )
                await fb_repo.get_or_create_source_config(
                    conn,
                    source_id=source.id,
                    auth_profile_id=auth_profile.id,
                    url=fb_source.url,
                    scan_times=fb_source.scan_times,
                    timezone=fb_source.timezone,
                )

                if existing is None:
                    created += 1
                elif _differs(existing, source):
                    updated += 1
                else:
                    unchanged += 1

                bindings_created += await self._bind(conn, source.id, edition.id)

        return BootstrapResult(
            sources_created=created,
            sources_updated=updated,
            sources_unchanged=unchanged,
            sources_skipped_db_managed=skipped,
            bindings_created=bindings_created,
            edition_created=edition_created,
        )

    async def _ensure_edition(self, conn: psycopg.AsyncConnection) -> tuple[Edition, bool]:
        """Return ``(edition, created)`` for the default edition.

        Missing editions are created with the berdyansk defaults: name
        'Бердянск', timezone 'Europe/Kyiv', language 'ru', enabled.
        """
        edition = await self._editions.get_by_slug(conn, DEFAULT_EDITION_SLUG)
        if edition is not None:
            return edition, False
        created = await self._editions.create(
            conn,
            NewEdition(
                slug=DEFAULT_EDITION_SLUG,
                name="Бердянск",
                timezone="Europe/Kyiv",
                language="ru",
                enabled=True,
            ),
        )
        return created, True

    async def _bind(self, conn: psycopg.AsyncConnection, source_id: int, edition_id: int) -> int:
        """Bind source to edition; returns 1 when a new binding row was made."""
        cursor = await conn.execute(
            """
            INSERT INTO source_editions(source_id, edition_id)
            VALUES (%s, %s)
            ON CONFLICT (source_id, edition_id) DO NOTHING
            """,
            (source_id, edition_id),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def _differs(existing: SimpleNamespace, source: Source) -> bool:
    """Compare only the fields bootstrap is allowed to overwrite."""
    return bool(
        existing.name != source.name
        or existing.url != source.url
        or existing.role != source.role
        or existing.enabled != source.enabled
        or existing.collector_options != source.collector_options
    )


class CollectorRegistry:
    """Selects the collector for a Source platform.

    This module owns all ingestion registries: :class:`SourceRegistry` maps
    configuration onto source rows, this class maps source platforms onto
    collectors. Factories are lazy (a collector may need provider credentials)
    and their instances are cached per platform, so one scan process builds
    each client once. ``register`` replaces an existing mapping and discards
    the cached instance; unknown platforms raise ``LookupError``.
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Collector]] = {}
        self._instances: dict[str, Collector] = {}

    def register(self, platform: str, factory: Callable[[], Collector]) -> None:
        """Bind a collector factory to a platform, replacing any previous one."""
        self._factories[platform] = factory
        self._instances.pop(platform, None)

    def registered_platforms(self) -> frozenset[str]:
        """Platforms with a collector factory, for dispatcher pre-filtering."""
        return frozenset(self._factories)

    def select(self, platform: str) -> Collector:
        """Return (building and caching on first use) the platform collector."""
        if platform not in self._factories:
            raise LookupError(f"no collector registered for platform {platform!r}")
        if platform not in self._instances:
            self._instances[platform] = self._factories[platform]()
        return self._instances[platform]

    async def aclose(self) -> None:
        """Release every cached collector's provider client (worker shutdown).

        Collectors without ``aclose`` (test fakes, stateless adapters) are
        simply dropped from the cache.
        """
        for instance in list(self._instances.values()):
            closer = getattr(instance, "aclose", None)
            if closer is not None:
                await closer()
        self._instances.clear()


def _build_telegram_collector() -> Collector:
    """Build the Telegram collector from configuration; imports stay lazy."""
    from src.config_loader import load_config
    from src.providers.telegram import TelegramCollector

    return TelegramCollector(load_config())


def _build_facebook_collector() -> Collector:
    """Build the Facebook collector from configuration; imports stay lazy."""
    from src.config_loader import load_config
    from src.providers.facebook.collector import FacebookCollector
    from src.providers.facebook.runtime_policy import is_facebook_enabled

    cfg = load_config()
    if not is_facebook_enabled(cfg):
        raise LookupError("facebook integration is disabled by facebook.enabled=false")
    auth_root = getattr(cfg.facebook, "auth_root", "/var/lib/telebrief/auth")
    return FacebookCollector(auth_root=auth_root)


def build_default_collector_registry(config: Config | None = None) -> CollectorRegistry:
    """Production wiring: registers Telegram and (if enabled) Facebook collectors."""
    from src.providers.facebook.runtime_policy import is_facebook_enabled

    registry = CollectorRegistry()
    registry.register(PLATFORM_TELEGRAM, _build_telegram_collector)
    if is_facebook_enabled(config):
        registry.register("facebook", _build_facebook_collector)
    return registry
