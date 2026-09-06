# Multisource Persistence and Orchestration Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the PostgreSQL 18 + pgvector + Psycopg 3 async + Procrastinate foundation and initial Source/Edition domain without changing current digest/article behavior.

**Architecture:** Introduce a new SQL-first domain persistence stack beside the legacy `src/storage.py`. Application services own Psycopg transactions and pass the same `psycopg.AsyncConnection` to thin repositories and Procrastinate when a domain write must atomically enqueue the next job. Procrastinate lives in a separate PostgreSQL schema and remains infrastructure rather than domain state.

**Tech Stack:** Python 3.14+, PostgreSQL 18, pgvector, psycopg 3, psycopg_pool, pgvector-python, Procrastinate, pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-multisource-knowledge-publication-architecture-design.md`

## Global Constraints

- New domain persistence is PostgreSQL-only; do not add a new SQLite implementation.
- Keep `src/storage.py` untouched except for compatibility imports until later cutover plans.
- Use explicit SQL migrations; runtime table creation is forbidden for the new domain.
- Repositories accept `psycopg.AsyncConnection` and never call `commit()` or obtain their own write connection.
- Use `psycopg_pool.AsyncConnectionPool` with default `min_size=1`, `max_size=4`.
- Register pgvector types on pool connections.
- Procrastinate uses a distinct PostgreSQL schema (default `procrastinate`).
- No Telebrief-owned queue/lease tables.
- Current digest/article tests must remain behaviorally unchanged in this plan.

---

## File structure locked by this plan

Create:

```text
migrations/
  0001_extensions.sql
  0002_domain_foundation.sql
  0003_collection_bookkeeping.sql
src/db/
  __init__.py
  pool.py
  migrations.py
  schema_version.py
  uow.py
src/domain/
  __init__.py
  sources.py
  editions.py
src/repositories/
  __init__.py
  sources.py
  editions.py
src/jobs/
  __init__.py
  app.py
  maintenance.py
src/bootstrap.py
src/runtime.py
scripts/migrate.py
tests/db/
  test_migrations.py
  test_pool.py
  test_transactional_defer.py
tests/repositories/
  test_sources.py
  test_editions.py
```

Modify:

```text
pyproject.toml
requirements.txt
uv.lock
src/config_loader.py
main.py
tests/conftest.py
.env.example
config.yaml.example
```

`src/storage.py` remains legacy and is not reused for the new domain.

### Task 1: Add database/orchestration dependencies and configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `uv.lock`
- Modify: `src/config_loader.py`
- Modify: `.env.example`
- Modify: `config.yaml.example`
- Test: `tests/test_config_loader.py`

**Interfaces:**
- Produces: `DatabaseConfig(enabled: bool, url: str, min_pool_size: int, max_pool_size: int, domain_schema: str, procrastinate_schema: str)`
- Produces: `load_database_config(path: str = "config.yaml", *, require_enabled: bool = False) -> DatabaseConfig`, which reads only the database YAML block + `DATABASE_URL` and does not require Telegram/AI credentials.
- Produces: `Config.database: DatabaseConfig`; full `load_config()` delegates to the same helper.
- No caller may log `database.url`.

- [ ] **Step 1: Write failing config tests**

Add tests asserting `database.enabled` defaults to `False` during the migration phase, a configured enabled database block is parsed, defaults are `min_pool_size=1`, `max_pool_size=4`, `domain_schema="public"`, `procrastinate_schema="procrastinate"`, and invalid pool sizes fail. Existing installations without `DATABASE_URL` must keep starting while `database.enabled=false`; `require_enabled=True` must reject disabled/missing-URL configuration with a clear error.

```python
def test_database_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    config = load_config(path=_write_minimal_config(tmp_path))
    assert config.database.min_pool_size == 1
    assert config.database.max_pool_size == 4
    assert config.database.domain_schema == "public"
    assert config.database.procrastinate_schema == "procrastinate"


def test_database_pool_max_must_cover_min(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    path = _write_config(tmp_path, {"database": {"min_pool_size": 4, "max_pool_size": 2}})
    with pytest.raises(ValueError, match="max_pool_size"):
        load_config(path=path)
```

- [ ] **Step 2: Run targeted tests and confirm failure**

Run:

```bash
pytest tests/test_config_loader.py -q -k database
```

Expected: FAIL because `DatabaseConfig` / `Config.database` do not exist.

- [ ] **Step 3: Add dependencies and minimal config implementation**

In both `pyproject.toml` and the runtime `requirements.txt`, add compatible current releases without removing legacy `aiosqlite`/`asyncpg` yet; regenerate `uv.lock` from the updated project metadata:

```toml
"psycopg[binary]>=3.3.4",
"psycopg-pool>=3.3.1",
"pgvector>=0.5.0",
"procrastinate>=3.9.0",
```

Mirror the runtime dependencies in `requirements.txt`:

```text
psycopg[binary]>=3.3.4
psycopg-pool>=3.3.1
pgvector>=0.5.0
procrastinate>=3.9.0
```

Then regenerate the project lock:

```bash
uv lock
```

Add:

```python
@dataclass
class DatabaseConfig:
    enabled: bool = False
    url: str = field(default="", repr=False)
    min_pool_size: int = 1
    max_pool_size: int = 4
    domain_schema: str = "public"
    procrastinate_schema: str = "procrastinate"
```

Load the URL from `DATABASE_URL`; require it when `enabled=true` or `require_enabled=True`. Validate `1 <= min_pool_size <= max_pool_size <= 10` for the initial host profile. `Config.database` is always present, but Plan 1's `TelebriefApp` only opens the new infrastructure when `config.database.enabled` is true.

- [ ] **Step 4: Run config tests and formatting**

```bash
pytest tests/test_config_loader.py -q -k database
ruff check src/config_loader.py tests/test_config_loader.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt uv.lock src/config_loader.py tests/test_config_loader.py .env.example config.yaml.example
git commit -m "feat(db): add postgres domain configuration"
```

### Task 2: Add SQL-first migration runner and schema compatibility table

**Files:**
- Create: `migrations/0001_extensions.sql`
- Create: `migrations/0002_domain_foundation.sql`
- Create: `migrations/0003_collection_bookkeeping.sql`
- Create: `src/db/migrations.py`
- Create: `src/db/schema_version.py`
- Create: `scripts/migrate.py`
- Test: `tests/db/test_migrations.py`

**Interfaces:**
- Produces: `async def migrate(conn: psycopg.AsyncConnection, migrations_dir: Path) -> int`
- Produces: `async def require_schema_compatible(conn, *, minimum: int, maximum: int) -> int`
- Migration ledger table: `telebrief_schema_migrations(version integer primary key, name text not null, applied_at timestamptz not null default now())`.

- [ ] **Step 1: Write failing migration tests**

Environment-gate with `TELEBRIEF_TEST_DATABASE_URL`.

```python
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migrate_applies_each_version_once(pg_conn):
    version = await migrate(pg_conn, Path("migrations"))
    assert version >= 3
    again = await migrate(pg_conn, Path("migrations"))
    assert again == version


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_compatibility_rejects_old_database(pg_conn):
    await pg_conn.execute("TRUNCATE telebrief_schema_migrations")
    with pytest.raises(SchemaVersionError):
        await require_schema_compatible(pg_conn, minimum=3, maximum=3)
```

- [ ] **Step 2: Run tests and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db/test_migrations.py -q
```

Expected: FAIL due to missing modules/migrations.

- [ ] **Step 3: Implement migrations**

Before reading applied versions, `migrate()` uses safely quoted identifiers to `CREATE SCHEMA IF NOT EXISTS <domain_schema>` and bootstrap only the migration ledger with `CREATE TABLE IF NOT EXISTS <domain_schema>.telebrief_schema_migrations (version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())`. This bootstrap is part of the migration tool, never normal application startup.

`0001_extensions.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Do not create Procrastinate tables or its namespace from Telebrief numbered domain migrations; Task 5 bootstraps the infrastructure namespace immediately before applying Procrastinate's official schema.

The migration ledger itself is not repeated inside numbered migration files.

`0002_domain_foundation.sql` creates only provider-neutral foundation tables `sources`, `editions`, and `source_editions`. Relevance policy tables and `editions.current_relevance_policy_id` belong to Plan 3 migration `0005_relevance_claims.sql`, so the same concept is not defined twice. Required Source identity constraint:

`Source.role` is constrained to `official | local_media | community | individual | other`; `Source.platform`/`kind` remain strings so future providers do not require a schema enum migration. `Edition` contains `slug UNIQUE`, name, timezone, language, JSON profile/config, enabled flag, and timestamps. `source_editions` has `PRIMARY KEY(source_id, edition_id)` with cascading FK cleanup only for the binding row, never for source history.


```sql
CREATE UNIQUE INDEX uq_sources_platform_kind_external_id
ON sources(platform, kind, external_id)
WHERE external_id IS NOT NULL;
```

`0003_collection_bookkeeping.sql` creates `collection_checkpoints` and `collection_runs` only; SourceItem tables are intentionally Plan 2.

`migrate()` must sort files by numeric prefix, reject duplicate versions, set the transaction-local search path to the configured domain schema before executing unqualified Telebrief DDL, apply each normal migration in one transaction, and record the version only after SQL succeeds. Support the explicit file header `-- telebrief: non-transactional`: commit any prior transaction, temporarily enable connection autocommit, execute that migration outside a transaction (required for operations such as `CREATE INDEX CONCURRENTLY`), restore normal mode, then record the migration version in its own transaction. Non-transactional migration SQL must be written idempotently because execution and ledger recording cannot be one transaction.

- [ ] **Step 4: Verify idempotence and rollback**

Add a temporary failing migration in the test temp directory and assert its version is not recorded after failure. Run:

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db/test_migrations.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations src/db/migrations.py src/db/schema_version.py scripts/migrate.py tests/db/test_migrations.py
git commit -m "feat(db): add versioned sql migrations"
```

### Task 3: Add Psycopg async pool with pgvector registration

**Files:**
- Create: `src/db/pool.py`
- Create: `src/db/__init__.py`
- Test: `tests/db/test_pool.py`

**Interfaces:**
- Produces: `async def open_pool(config: DatabaseConfig) -> AsyncConnectionPool`
- Produces: `async def close_pool(pool: AsyncConnectionPool) -> None`
- Every returned connection has pgvector types registered.

- [ ] **Step 1: Write failing pool integration test**

```python
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pool_registers_pgvector(database_config):
    pool = await open_pool(database_config)
    try:
        async with pool.connection() as conn:
            row = await conn.execute("SELECT %s::vector AS v", ([1.0, 2.0, 3.0],))
            value = (await row.fetchone())[0]
            assert list(value) == [1.0, 2.0, 3.0]
    finally:
        await close_pool(pool)
```

- [ ] **Step 2: Run and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db/test_pool.py -q
```

- [ ] **Step 3: Implement pool**

Use `psycopg_pool.AsyncConnectionPool` with a configure callback. Quote configured schema identifiers with `psycopg.sql.Identifier` and set a search path that lets the same external connection address both Telebrief domain tables and Procrastinate tables:

```python
async def _configure_connection(conn: psycopg.AsyncConnection) -> None:
    await register_vector_async(conn)
    await conn.execute("SET TIME ZONE 'UTC'")
    await conn.execute(
        sql.SQL("SET search_path TO {}, {}, public").format(
            sql.Identifier(config.domain_schema),
            sql.Identifier(config.procrastinate_schema),
        )
    )
```

This search path is required because `Task.configure(connection=conn).defer_async(source_id=source.id)` executes Procrastinate SQL on the externally-managed domain connection.

Construct the pool with `min_size=config.min_pool_size`, `max_size=config.max_pool_size`, `open=False`, then `await pool.open()` and `await pool.wait()`.

- [ ] **Step 4: Run targeted and full DB tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db -q
```

- [ ] **Step 5: Commit**

```bash
git add src/db tests/db/test_pool.py
git commit -m "feat(db): add async psycopg pool"
```

### Task 4: Add Source and Edition domain models/repositories

**Files:**
- Create: `src/domain/sources.py`
- Create: `src/domain/editions.py`
- Create: `src/repositories/sources.py`
- Create: `src/repositories/editions.py`
- Test: `tests/repositories/test_sources.py`
- Test: `tests/repositories/test_editions.py`

**Interfaces:**
- Produces: `SourceRepository.create(conn, source: NewSource) -> Source`
- Produces: `SourceRepository.get(conn, source_id: int) -> Source | None`
- Produces: `SourceRepository.list_enabled(conn, *, platform: str | None = None) -> list[Source]`
- Produces: `EditionRepository.get_by_slug(conn, slug: str) -> Edition | None`
- Produces: `EditionRepository.bind_source(conn, source_id: int, edition_id: int) -> None`
- Repositories never commit.

- [ ] **Step 1: Write failing repository tests**

Cover source uniqueness, source-role validation by DB `CHECK`, and source-edition many-to-many binding.

```python
source = await repo.create(conn, NewSource(platform="telegram", kind="channel", external_id="-1001", name="News", role="community"))
with pytest.raises(psycopg.errors.UniqueViolation):
    await repo.create(conn, NewSource(platform="telegram", kind="channel", external_id="-1001", name="Duplicate", role="community"))
```

- [ ] **Step 2: Run and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/repositories/test_sources.py tests/repositories/test_editions.py -q
```

- [ ] **Step 3: Implement immutable dataclasses and explicit SQL repositories**

Use `%s` Psycopg placeholders and `RETURNING`. Domain dataclasses contain no DB methods. Example repository signature:

```python
async def create(self, conn: psycopg.AsyncConnection, source: NewSource) -> Source:
    cursor = await conn.execute(
        """INSERT INTO sources(platform, kind, external_id, url, name, role, enabled, collector_options)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id, platform, kind, external_id, url, name, role, enabled, collector_options, created_at, updated_at""",
        (
            source.platform, source.kind, source.external_id, source.url,
            source.name, source.role, source.enabled, Jsonb(source.collector_options),
        ),
    )
    return Source.from_row(await cursor.fetchone())
```

- [ ] **Step 4: Run tests**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/repositories -q
```

- [ ] **Step 5: Commit**

```bash
git add src/domain src/repositories tests/repositories
git commit -m "feat(domain): add source and edition repositories"
```

### Task 5: Add Procrastinate app and stalled-job maintenance task

**Files:**
- Create: `src/jobs/app.py`
- Create: `src/jobs/maintenance.py`
- Create: `src/jobs/__init__.py`
- Test: `tests/db/test_transactional_defer.py`

**Interfaces:**
- Produces: module-level `procrastinate_app: procrastinate.App`.
- Tests register their probe task from `tests/db/test_transactional_defer.py` through a test Blueprint; production code does not contain a no-op probe task.
- Produces periodic `retry_stalled_jobs` using documented `get_stalled_jobs()` + `retry_job()` behavior.

- [ ] **Step 1: Write failing transactional-defer integration test**

Define a test task via a Blueprint or dedicated test app. The critical assertion is job visibility in the Procrastinate tables after commit and absence after rollback.

```python
async with pool.connection() as conn:
    with pytest.raises(RuntimeError):
        async with conn.transaction():
            source = await sources.create(conn, new_source)
            await process_source.configure(connection=conn).defer_async(source_id=source.id)
            raise RuntimeError("force rollback")

assert await count_sources(pool) == 0
assert await count_procrastinate_jobs(procrastinate_app) == 0
```

- [ ] **Step 2: Run and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db/test_transactional_defer.py -q
```

- [ ] **Step 3: Implement Procrastinate connector and periodic recovery**

Create the app using the documented PostgreSQL search-path configuration for Procrastinate 3.9:

```python
database_config = load_database_config()
connector = procrastinate.PsycopgConnector(
    conninfo=database_config.url,
    min_size=1,
    max_size=2,
    kwargs={"options": f"-c search_path={database_config.procrastinate_schema}"},
)
procrastinate_app = procrastinate.App(
    connector=connector,
    import_paths=("src.jobs.maintenance",),
)
```

`src/jobs/app.py` must use `load_database_config(require_enabled=True)` rather than full `load_config()` so Procrastinate CLI commands do not require Telegram/API credentials and fail clearly when the DB worker is invoked before the migration flag is enabled. The connector pool is intentionally capped at 2 connections; deployment must account for the total connection budget across app and worker processes.

Create only the infrastructure namespace, then apply the **official Procrastinate schema** separately before the integration test:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'CREATE SCHEMA IF NOT EXISTS procrastinate;'
PYTHONPATH=. procrastinate --app=src.jobs.app.procrastinate_app schema --apply
```

When `database.procrastinate_schema` is customized, the bootstrap command must safely quote that configured identifier (use a tiny Psycopg admin helper rather than shell interpolation). The official Procrastinate command remains the owner of the tables/functions inside the namespace.

For later Procrastinate upgrades, follow its shipped pre/post SQL migrations; do not add those SQL files to Telebrief numbered migrations. Register:

```python
@procrastinate_app.periodic(cron="*/10 * * * *")
@procrastinate_app.task(queueing_lock="retry_stalled_jobs", pass_context=True)
async def retry_stalled_jobs(context, timestamp):
    stalled_jobs = await procrastinate_app.job_manager.get_stalled_jobs()
    for job in stalled_jobs:
        await procrastinate_app.job_manager.retry_job(job)
```

- [ ] **Step 4: Verify commit and rollback behavior**

Add a successful case to the same test and assert both source + job become visible only after commit.

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db/test_transactional_defer.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/jobs tests/db/test_transactional_defer.py
git commit -m "feat(jobs): add procrastinate orchestration foundation"
```

### Task 6: Add Unit-of-Work helper without hiding transaction ownership

**Files:**
- Create: `src/db/uow.py`
- Test: `tests/db/test_uow.py`

**Interfaces:**
- Produces: `DatabaseUnitOfWork.transaction()` async context manager yielding `psycopg.AsyncConnection`.
- It may acquire/release a pool connection and open/close the transaction, but repositories still receive the yielded connection explicitly.

- [ ] **Step 1: Write failing rollback test**

```python
@pytest.mark.asyncio
async def test_uow_rolls_back_on_exception(uow, source_repo):
    with pytest.raises(ValueError):
        async with uow.transaction() as conn:
            await source_repo.create(conn, NEW_SOURCE)
            raise ValueError("boom")
    assert await _source_count(uow.pool) == 0
```

- [ ] **Step 2: Run and confirm failure**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db/test_uow.py -q
```

- [ ] **Step 3: Implement minimal UoW**

```python
class DatabaseUnitOfWork:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn
```

Do not add repository attributes or implicit global connections.

- [ ] **Step 4: Run test**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db/test_uow.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/db/uow.py tests/db/test_uow.py
git commit -m "feat(db): add explicit transaction unit of work"
```

### Task 7: Bootstrap DB/Procrastinate lifecycle in the application without changing publication path

**Files:**
- Create: `src/bootstrap.py`
- Create: `src/runtime.py`
- Modify: `main.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_main_bootstrap.py`

**Interfaces:**
- Produces: `ApplicationInfrastructure(pool, uow, procrastinate_app)` with `open()` / `close()` lifecycle and `async def build_infrastructure(database_config: DatabaseConfig) -> ApplicationInfrastructure`.
- Produces process-local runtime registry `install_runtime(infrastructure)`, `get_runtime() -> ApplicationInfrastructure`, `clear_runtime(infrastructure)` for task/application-service dependency access. The registry owns no connections itself; repositories still receive explicit transaction connections.
- `TelebriefApp.initialize()` must fail fast on incompatible domain schema.
- Existing `DigestScheduler`, bot handler, MCP, and `core.py` remain the behavior owners in this plan.

- [ ] **Step 1: Write failing lifecycle tests**

Test both modes. With `database.enabled=false`, verify the existing scheduler/bot/MCP initialize without opening PostgreSQL. With `database.enabled=true`, verify order: load config -> open DB -> schema check -> open Procrastinate -> install process runtime -> initialize existing scheduler/bot. Verify `get_runtime()` raises a clear `RuntimeError` before installation, returns the exact infrastructure while running, and shutdown clears runtime then closes Procrastinate/DB even if one component fails.

- [ ] **Step 2: Run targeted tests**

```bash
pytest tests/test_main_bootstrap.py -q
```

- [ ] **Step 3: Implement infrastructure bootstrap**

`src/bootstrap.py` should construct the pool, call `require_schema_compatible`, and open the Procrastinate app. While `database.enabled=false`, it must not import `src.jobs.app` at module import time; load the Procrastinate app lazily inside the enabled branch so legacy Plan-1 startup does not require `DATABASE_URL`. `src/runtime.py` exposes only a guarded process-local reference:

```python
_runtime: ApplicationInfrastructure | None = None

def install_runtime(infrastructure: ApplicationInfrastructure) -> None:
    global _runtime
    if _runtime is not None:
        raise RuntimeError("Telebrief runtime already installed")
    _runtime = infrastructure

def get_runtime() -> ApplicationInfrastructure:
    if _runtime is None:
        raise RuntimeError("Telebrief runtime is not initialized")
    return _runtime
```

`main.py` stores optional `self.infrastructure`; when database support is enabled it installs the runtime after successful open/schema check and clears it during graceful shutdown. When disabled it leaves the legacy application behavior unchanged. Do not start a Procrastinate worker in the main bot process yet; deployment/worker process is added only after tasks exist in Plan 2.

- [ ] **Step 4: Run full non-Postgres suite**

```bash
pytest -q
ruff check src tests main.py
ruff format --check src tests main.py
mypy src main.py
```

Expected: existing digest/article behavior remains green.

- [ ] **Step 5: Run PostgreSQL integration suite and commit**

```bash
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db tests/repositories -q
git add src/bootstrap.py src/runtime.py main.py tests/conftest.py tests/test_main_bootstrap.py
git commit -m "feat(app): bootstrap domain database infrastructure"
```

## Plan 1 completion gate

Before moving to Plan 2, verify:

```bash
pytest -q
ruff check src tests main.py
ruff format --check src tests main.py
mypy src main.py
TELEBRIEF_TEST_DATABASE_URL=postgresql://telebrief:telebrief@localhost:5432/telebrief_test pytest tests/db tests/repositories -q
```

Manual DB checks:

```sql
SELECT extname FROM pg_extension WHERE extname='vector';
SELECT version, name FROM telebrief_schema_migrations ORDER BY version;
SELECT schema_name FROM information_schema.schemata WHERE schema_name='procrastinate';
```

The current digest/article path must still work exactly as before. No source collection or publication cutover belongs in this plan.
