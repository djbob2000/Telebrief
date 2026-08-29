# AGENTS.md

> Comprehensive operational, architectural, and technical reference for AI coding agents working on **Telebrief**.

---

## 1. System Overview & Core Architecture

**Telebrief** is an automated, AI-powered Telegram digest and editorial publication engine. It continuously aggregates news, announcements, and alerts from diverse platforms (Telegram channels, Facebook public pages, RSS feeds, and web sources), extracts and verifies factual claims, groups related events using semantic vector embeddings, enhances stories with municipal/regional knowledge, synthesizes balanced briefs and long-form articles using multi-provider LLM cascades, and publishes formatted outputs to Telegram channels and Telegra.ph.

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                   INGESTION PIPELINE                    │
                    │  Telegram (Telethon) | Facebook (Playwright) | RSS/Web  │
                    └────────────────────────────┬────────────────────────────┘
                                                 │ raw source items
                                                 ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │                  PROCESSING & KNOWLEDGE                 │
                    │   • Relevance Filter & Text Cleaning                    │
                    │   • Factual Claim Extraction & Evidence Linking         │
                    │   • Vector Embeddings (OpenAI / Gemini / Ollama)        │
                    │   • Story Clustering & Incremental Matching             │
                    │   • Regional / Municipal Context Enrichment             │
                    └────────────────────────────┬────────────────────────────┘
                                                 │ verified story clusters
                                                 ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │               EDITORIAL SYNTHESIS ENGINE                │
                    │   • Multi-Provider LLM Cascade (OpenAI/Anthropic/Gemini)│
                    │   • Editorial Tone, Fact-Checking & Bias Audit          │
                    │   • Multi-Pass Rewriting & Formatting                   │
                    │   • AI Illustration Generation                          │
                    └────────────────────────────┬────────────────────────────┘
                                                 │ formatted publication
                                                 ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │                  DELIVERY & PUBLICATION                 │
                    │      Telegram Bot Channels/Groups  |  Telegra.ph API     │
                    └─────────────────────────────────────────────────────────┘
```

---

## 2. Technology Stack Reference

| Subsystem | Technology | Details / Notes |
|---|---|---|
| **Language & Runtime** | Python 3.14+ | Modern type syntax (`A \| B`, `list[str]`, `dict[str, Any]`), strict async |
| **Async Framework** | `asyncio`, `aiohttp`, `aiosqlite` | Non-blocking event loops across all network and I/O operations |
| **Database & Vectors** | PostgreSQL 18 + `pgvector` | `psycopg3` (binary) + `psycopg-pool` with `DatabaseUnitOfWork` pattern |
| **Background Task Queue** | `procrastinate` (v3.9+) | PostgreSQL-backed distributed queue with periodic cron scheduling |
| **Telegram Integration** | `telethon` & `python-telegram-bot` | Telethon for scraping channels via MTProto; PTB for bot commands |
| **LLM & Embeddings** | Multi-provider Cascade | OpenAI, Anthropic, Google Gemini, DeepSeek, Mistral, Ollama, OpenRouter |
| **Dynamic Web Scraping** | `playwright` (Chromium) | Headless scraping with persistent browser profiles for Facebook |
| **Landing Website** | Astro 4 (`website/` directory) | Static site generator deployed to Cloudflare Pages |
| **MCP Server** | Model Context Protocol 2.0 | Streamable HTTP server (`src/mcp_server.py`) for AI tool integration |
| **Code Quality** | Ruff, MyPy, Pre-commit | Target py313+, strict linting (`E, W, F, I, B, C4, S`), 100-char line limit |

---

## 3. Codebase Map (`src/` Directory Structure)

```
src/
├── ai_providers.py         # Multi-provider LLM wrappers, cooldowns & failover cascade
├── article_generator.py    # Long-form editorial article synthesizer & coordinator
├── bootstrap.py            # Infrastructure bootstrap (pool, schema gating, Procrastinate)
├── bot_commands.py         # Telegram bot handlers (/digest, /cleanup, /status, /help)
├── city_context.py         # Municipal context (Berdyansk utilities, landmarks, entities)
├── city_context_models.py  # Domain data models for city entities and utilities
├── collector.py            # Legacy/direct message collector orchestrator
├── config_loader.py        # YAML config loader, dataclass validation & env overrides
├── core.py                 # Core workflow coordination (digest & article compilation)
├── db/
│   ├── migrations.py       # SQL migration runner and ledger tracker
│   ├── pool.py             # psycopg AsyncConnectionPool lifecycle management
│   ├── schema_version.py   # Schema version compatibility validator (v7 to v20)
│   └── uow.py              # DatabaseUnitOfWork transaction context manager
├── domain/                 # Pure domain models (claims, evidence, stories, sources)
├── editorial_analysis.py   # Tone, balance, structure, and editorial verification
├── editorial_audit.py      # Quality assurance audits and factual compliance checks
├── editorial_fallback.py   # Safe template-based fallback generator when LLMs fail
├── editorial_input.py      # Input packaging and prompt preparation for LLM synthesis
├── editorial_models.py     # Editorial dataclasses (Draft, Review, Publication, Rubric)
├── editorial_writer.py     # Multi-pass editorial rewriting & tone styling engine
├── embedding_providers.py  # Vector embedding providers (OpenAI, Gemini, Ollama)
├── extensions/             # Extensible plugin interfaces (filters, custom prompts)
├── formatter.py            # Telegram HTML/Markdown and Telegraph formatters
├── grouper.py              # Semantic message clustering, embeddings & topic grouping
├── historical_context.py   # Historical background injection for recurring stories
├── image_generator.py      # AI editorial illustration generator (Gemini / Imagen)
├── ingestion/              # Ingestion protocols, registry, models, and readers
├── jobs/                   # Procrastinate tasks (collection, processing, publication, maintenance)
├── mcp_server.py           # Model Context Protocol integration server
├── processing/             # Pipeline processors (claims, places, relevance, vision, matching)
├── prompts/                # Prompt templates for LLM generation
├── providers/              # Integration provider adapters (Facebook, Telegram)
├── publication/            # Publication dispatchers, renderers & channel routers
├── repositories/           # PostgreSQL repository layer (SQL queries & vector search)
├── retention.py            # Historical data retention policies & automated cleanup
├── runtime.py              # Global process-level runtime container (get_runtime())
├── scheduler.py            # In-process background scheduler for standalone mode
├── sender.py               # Telegram bot sender & Telegra.ph client
├── storage.py              # Local/database persistence layer
├── summarizer.py           # Digest summarization and deduplication
├── telegraph.py            # Telegra.ph API client wrapper
├── ui_strings.py           # Multi-language string catalogs (RU, UK, EN)
├── utils.py                # Helper utilities (timezones, text cleaning, logging)
├── worker.py               # Procrastinate background worker entry point
└── xml_escape.py           # XML/HTML escaping utilities
```

---

## 4. Setup & Installation Commands

### 1. Environment & Dependencies

```bash
# 1. Initialize Python 3.14 virtual environment
python3.14 -m venv .venv
source .venv/bin/activate

# 2. Upgrade pip and install all development dependencies
pip install --upgrade pip
pip install -r requirements-dev.txt

# 3. Install Playwright browser binaries (required for Facebook ingestion)
playwright install chromium

# 4. Install git pre-commit hooks
pre-commit install

# 5. Prepare configuration files
cp .env.example .env
cp config.yaml.example config.yaml
```

### 2. Telegram Session Authentication

To ingest private and public channels via userbot MTProto:

```bash
# Interactively log in to Telegram and create sessions/telebrief.session
python create_session.py
# (or execute: ./create_session.sh)
```

### 3. Database & Migrations

Telebrief requires PostgreSQL 18 with `pgvector` enabled:

```bash
# Start PostgreSQL via Docker Compose
docker compose up -d postgres

# Apply database schema migrations (migrations/0001_*.sql to 0020_*.sql)
python scripts/migrate.py

# Initialize Procrastinate official schema tables
python -c "import asyncio, os; from src.jobs.admin import ensure_official_tables; asyncio.run(ensure_official_tables(os.environ.get('DATABASE_URL', 'postgresql://telebrief:password@localhost:5432/telebrief'), 'procrastinate'))"
```

---

## 5. Development Workflow & CLI Commands

### Execution Modes

- **Daemon Mode (Bot + Scheduler + MCP Server)**:
  ```bash
  python main.py
  ```

- **Generate Digest On-Demand (CLI)**:
  ```bash
  # Trigger digest compilation and publication for past 24 hours (or specify --hours N)
  python main.py --digest --hours 24

  # Dry-run: preview formatted digest in terminal without publishing
  python main.py --digest --dry-run
  ```

- **Generate Long-Form Editorial Article On-Demand (CLI)**:
  ```bash
  # Trigger editorial article generation and publication for past 48 hours
  python main.py --article --hours 48

  # Dry-run article generation
  python main.py --article --dry-run
  ```

### Background Job Worker (`procrastinate`)

> [!IMPORTANT]
> **Always run the worker via `python -m src.worker`**, never using the bare `procrastinate ... worker` CLI command.
> `src.worker` initializes `ApplicationInfrastructure` and sets up `src.runtime.install_runtime()`, which all Procrastinate task bodies rely upon via `src.runtime.get_runtime()`.

```bash
# Start the background worker across all queues with 2 concurrent workers
python -m src.worker --concurrency=2
```

**Worker Queues Served**:
- `collection`: Ingestion scans for Telegram, Facebook, and RSS sources.
- `processing`: Factual claim extraction, vision parsing, embeddings, and story matching.
- `publication`: Snapshot compilation, editorial synthesis, and Telegram/Telegraph delivery.
- `maintenance`: Retention cleanup, archive sweeps, and schedule dispatchers.
- `enrichment`: Facebook comment refreshes and secondary context updates.
- `default`: Miscellaneous asynchronous tasks.

### Full Stack Docker Compose

```bash
# Build and start all services (telebrief-app, telebrief-worker, postgres)
docker compose up -d --build

# Inspect logs
docker compose logs -f telebrief-app telebrief-worker postgres
```

### Static Website Subproject (`website/`)

```bash
cd website
npm install
npm run dev       # Starts local Astro dev server at http://localhost:4321
npm run build     # Builds production static bundle into website/dist/
npm run preview   # Previews the production build
```

---

## 6. Testing Instructions & Test Isolation

Telebrief uses `pytest` with `pytest-asyncio`, `pytest-cov`, `pytest-mock`, and `pytest-timeout`.

### Running Tests

- **Run all tests with full coverage report**:
  ```bash
  pytest
  # or
  make test
  ```
  *(Note: Pytest enforces a strict coverage threshold of **49%** via `--cov-fail-under=49` in `pytest.ini`.)*

- **Fast test execution (Skip coverage calculation)**:
  ```bash
  pytest -v --no-cov
  # or
  make test-fast
  ```

- **Run a single test file (CRITICAL: Always append `--no-cov`)**:
  ```bash
  # When running a single file, --no-cov prevents failure from the global 49% coverage threshold
  pytest tests/test_config_loader.py -v --no-cov
  pytest tests/test_grouper.py -k "test_semantic_clustering" -v --no-cov
  ```

- **Run specific test suites by marker**:
  ```bash
  pytest -v -m unit --no-cov             # Unit tests only
  pytest -v -m integration --no-cov      # Integration tests
  DATABASE_URL="postgresql://telebrief:telebrief@localhost:5432/telebrief_test" pytest -v -m postgres --no-cov
  ```

### Critical Test Isolation Fixtures (`tests/conftest.py`)

- **`reset_telebrief_runtime`** *(autouse)*: Clears `src.runtime._runtime` before and after every test to prevent runtime contamination between test cases.
- **`reset_provider_cascade_state`** *(autouse)*: Resets `ProviderCascade` global cooldowns and round-robin state across tests.
- **`database_config`**: Points to `TELEBRIEF_TEST_DATABASE_URL` (default: `postgresql://telebrief:telebrief@localhost:5432/telebrief_test`).

---

## 7. Code Style & Quality Standards

Telebrief enforces strict linting via **Ruff** and static type checking via **MyPy**.

### Quality Commands

```bash
# Run all linters and type checkers
make lint
# or:
ruff check src tests
ruff format --check src tests
mypy src

# Auto-format code and fix autofixable lint errors
make format
# or:
ruff format src tests
ruff check --fix src tests

# Run pre-commit checks across all repository files
pre-commit run --all-files
```

### Key Python & Architectural Rules

1. **Python 3.14+ Syntax**:
   - Use `int | None`, `str | Path` instead of `Optional[...]` / `Union[...]`.
   - Use lowercase standard generics (`list[str]`, `dict[str, Any]`, `tuple[int, ...]`) instead of `typing.List`, `typing.Dict`.
2. **Formatting**:
   - Double quotes for strings (`quote-style = "double"`).
   - Maximum line length: **100 characters**.
   - Indentation: 4 spaces.
3. **Database Transactions & Pool Safety**:
   - **Always** acquire connections using `async with runtime.uow.transaction() as conn:` or `async with pool.connection() as conn:`.
   - Never leak connections; connection pool limit is constrained (`max_connections=10`).
   - Repositories in `src/repositories/` must accept an explicit `conn: psycopg.AsyncConnection` parameter.
4. **Async Loop Safety**:
   - **Never** call blocking functions (`time.sleep()`, synchronous `requests`, heavy blocking I/O) in `async def` functions.
   - Use `asyncio.sleep()` or non-blocking async clients (`aiohttp`).
5. **Runtime Container Pattern**:
   - Never instantiate ad-hoc `AsyncConnectionPool` inside Procrastinate tasks or service functions. Always retrieve shared resources via `src.runtime.get_runtime()`.

---

## 8. Database Migrations & Schema Architecture

- **Migration files**: Located in `migrations/0001_*.sql` through `migrations/0023_*.sql`.
- **Ledger table**: `schema_migrations` tracks applied versions and timestamps.
- **Schema version gating**:
  - Defined in `src/bootstrap.py`: `SCHEMA_VERSION_MINIMUM = 7`, `SCHEMA_VERSION_MAXIMUM = 23`.
  - Application startup automatically verifies schema compatibility and fails fast (`SchemaVersionError`) if migrations are missing.
- **Running migrations**:
  ```bash
  python scripts/migrate.py
  ```
- **Creating a new migration**:
  - Create `migrations/00XX_short_description.sql`.
  - Use idempotent DDL (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`).
  - Update `SCHEMA_VERSION_MAXIMUM` in `src/bootstrap.py` if adding a new migration step.

---

## 8.1 Event-First Rich Analysis Pipeline

The Event-First architecture optimizes knowledge-processing spend, throughput, and clustering accuracy:

1. **Deterministic Fragmentation & Noise Filtering** (`src/processing/fragments.py`):
   - Chunks incoming messages into discrete factual assertions and filters chatter/classifieds.
2. **Deduplicated Vector Embeddings** (`src/processing/embeddings.py`):
   - Computes normalized vector embeddings with sha256 hash caching (`fragment_embeddings`).
3. **Streaming Centroid Story Clustering** (`src/processing/event_clustering.py`):
   - Matches incoming fragments against open story cluster centroids with temporal decay and dynamic recalculation (`story_cluster_state`, `story_fragments`).
4. **Representative Evidence Sampling** (`src/processing/evidence_sampling.py`):
   - Uses Maximal Marginal Relevance (MMR, $\lambda=0.7$) with official source boosting and multi-source diversity.
5. **Gate V2 Batch Triage & Brief Synthesis** (`src/processing/event_triage.py`, `src/processing/event_brief.py`):
   - Batches candidate stories into unified Gate V2 triage evaluating geographic scope (`LOCAL`, `DIRECT_IMPACT`, `OUT_OF_SCOPE`, `UNCERTAIN`), retention (`KEEP`, `DROP`), and enrichment (`NONE`, `BRIEF`, `ANALYZE`).
   - Immediately synthesizes and persists `BRIEF` `EventPayload` revisions (`story_revisions`) without secondary LLM invocations.
   - Preserves cached triage decisions across cycles with fingerprint and config-hash validation.
6. **Rich Event Analysis & Operational Intelligence** (`src/processing/event_analysis.py`, `src/domain/event_payload.py`, `src/domain/operational_state.py`):
   - Extracts structured `event_payload` and discrete `OperationalObservationPayload`s with exact fragment provenance IDs.
   - Resolves recurring operational states through pure chronological temporal aggregation (`resolve_operational_states()`).
7. **Publication Candidate Eligibility & Digest Rollup** (`src/publication/repository.py`, `src/publication/city_situation.py`, `src/publication/renderers.py`, `src/publication/event_editorial_adapter.py`):
   - Enforces strict Gate V2 `KEEP` retention and local scope eligibility on snapshot candidates.
   - Assembles digest-only `CitySituationRollup` displaying point-in-time operational statuses (🟢/🔴/🟡).
   - Renders truthful 4-level publication statistics (sources, messages, facts, events) in formatted Telegram HTML digests.

**Event Pipeline Scripts & Benchmarks**:
- **Offline Quality & Cost Evaluator**:
  ```bash
  python scripts/evaluate_event_pipeline.py --fixture tests/fixtures/event_first_day.json
  ```
- **Historical Backfill CLI**:
  ```bash
  python scripts/backfill_events.py --hours 72 --batch-size 32
  ```
- **Bounded Story Rescreen CLI**:
  ```bash
  python scripts/rescreen_stories.py --hours 72 --edition berdyansk --batch-size 80
  ```
- **Golden Regression Oracle**:
  ```bash
  pytest tests/integration/test_city_situation_golden.py -v --no-cov
  ```

---



## 9. AI Providers & Configuration Precedence

The AI subsystem (`src/ai_providers.py`, `src/config_loader.py`) implements a resilient multi-provider setup with automatic configuration sync:

1. **Configuration Precedence (12-Factor App)**:
   - **Provider Resolution**: `AI_PROVIDER` env var > `config.yaml` (`settings.ai_provider`) > default (`openai`).
   - **Model Resolution**:
     - Generic override: `AI_MODEL` env var.
     - Provider-specific env vars: `OPENROUTER_MODEL` (when `ai_provider: openrouter`), `OPENAI_MODEL` (when `ai_provider: openai`).
     - Configuration file: `config.yaml` (`settings.ai_model`).
     - Fallback: Built-in provider default (e.g. `openrouter/free`, `gpt-5-nano`, `gemini-3.6-flash`).
   - Setting `OPENROUTER_MODEL` in `.env` automatically syncs across all synthesis stages (digests, long-form articles, claim extraction, selection, editorial audit).

2. **Supported Providers**: OpenRouter, OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Mistral, Ollama (local).
3. **Failover & Cooldowns**:
   - When a provider hits a rate limit (`429`), quota exhaustion, or server error (`5xx`), it is put on a temporary cooldown.
   - The cascade automatically fails over to the next configured provider slot.
   - Fallback text generation (`src/editorial_fallback.py`) activates if all AI providers in the cascade fail.

---

## 10. Security & Secrets Management

- **Zero Secret Commits**: Never commit `.env`, API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`), bot tokens (`TELEGRAM_BOT_TOKEN`), or database credentials.
- **Telegram Auth**: Files in `sessions/` and docker volume `telebrief_auth` contain sensitive MTProto session hashes; never expose them.
- **Automated Security Scans**:
  - Ruff Bandit rules: `ruff check --select S src/`
  - Dependency vulnerability scan: `safety check`

---

## 11. Pull Request & CI/CD Guidelines

Before submitting changes or opening a PR:

1. **Format & Lint**: Run `make format && make lint`.
2. **Run Tests**: Ensure all tests pass with `make test` (and verify coverage >= 49%).
3. **Verify Pre-commit**: Execute `pre-commit run --all-files`.
4. **CI Workflow Verification** (`.github/workflows/ci.yml`):
   - `lint`: Pre-commit checks.
   - `test`: Pytest suite with live PostgreSQL + pgvector service container.
   - `security`: Ruff security scan + Safety.
   - `website-build`: Node.js 24 + Astro build (`website/`).
   - `build`: Python package build verification (`python -m build`).
5. **Commit Message Format**:
   - `feat(scope): add new feature`
   - `fix(scope): fix bug or edge case`
   - `refactor(scope): refactor internal module`
   - `test(scope): add or update test coverage`
   - `docs(scope): update documentation or AGENTS.md`

---

## 12. Troubleshooting & Common Gotchas

1. **`RuntimeError: Telebrief runtime is not initialized`**:
   - Cause: Running a Procrastinate worker via the bare CLI `procrastinate ... worker` instead of `python -m src.worker`.
   - Fix: Always use `python -m src.worker [--concurrency=N]`.

2. **`FAIL Required test coverage of 49% not reached` when running a single test**:
   - Cause: `pytest.ini` defines `--cov-fail-under=49` by default for the full test suite.
   - Fix: Add `--no-cov` when executing individual test files (e.g. `pytest tests/test_foo.py --no-cov`).

3. **Database Connection Pool Exhaustion**:
   - Cause: Unclosed connection or long-running queries outside `async with runtime.uow.transaction() as conn:`.
   - Fix: Always wrap connection acquisition in async context managers.

4. **Telegram `FloodWaitError`**:
   - Cause: Telegram rate limits when querying channel history too rapidly.
   - Fix: Handlers in `src/collector.py` and `src/ingestion/` automatically respect exponential backoff and `seconds` parameters from the exception.
