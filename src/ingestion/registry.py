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
  timestamps) and are left completely untouched.
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

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from src.config_loader import Config
from src.domain.editions import NewEdition
from src.domain.sources import NewSource, Source
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
    conn: psycopg.AsyncConnection, external_id: str
) -> SimpleNamespace | None:
    """Read the mutable-by-bootstrap fields plus the ownership mode."""
    cursor = await conn.execute(
        """
        SELECT name, url, role, enabled, collector_options, management_mode
        FROM sources
        WHERE platform = %s AND kind = %s AND external_id = %s
        """,
        (PLATFORM_TELEGRAM, KIND_CHANNEL, external_id),
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
        """Upsert every configured Telegram channel and bind it to the edition.

        Ensures the default ``berdyansk`` edition first, then walks the
        configured channels in order: DB-managed rows are counted and skipped,
        bootstrap-managed rows are refreshed through the guarded upsert, and
        each managed row is bound to the edition (binding itself is
        idempotent). Never commits.
        """
        created = updated = unchanged = skipped = 0

        edition, edition_created = await self._ensure_edition(conn)

        interval_minutes = config.collection.telegram_interval_minutes
        bindings_created = 0

        for channel in config.channels:
            external_id = str(channel.id)
            existing = await _fetch_existing(conn, external_id)
            if existing is not None and existing.management_mode == "database":
                skipped += 1
                continue

            candidate = build_bootstrap_source(channel, interval_minutes)
            source = await upsert_bootstrap_source(conn, candidate)
            if existing is not None and existing.management_mode == "database":
                skipped += 1
                continue

            source = await upsert_bootstrap_source(conn, candidate)
            if source is None:
                # Raced flip to 'database' between the read and the upsert:
                # treat exactly like the pre-checked skip.
                skipped += 1
                continue

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

    async def _ensure_edition(self, conn: psycopg.AsyncConnection) -> tuple[Any, bool]:
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
