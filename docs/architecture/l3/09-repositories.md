---
id: "l3-repositories"
level: "L3"
eyebrow: "Уровень 3 · Платформа"
nav: "Репозитории и стек"
group: "Платформа"
---

## Репозитории и стек

Компоненты простыми словами — в [Основных компонентах](#l1-components); соответствие названий документа и кода — в [глоссарии](#l1-names).

::: tldr
**TL;DR для разработчика.** Один Python-репозиторий `Telebrief` (Python 3.14, async), один образ Docker на пять процессов Compose, PostgreSQL 18 с pgvector как единственное хранилище и очередь задач (Procrastinate). Модели — через OpenRouter; генеративные — строго из списка в `.env`. Эта документация лежит в `docs/` того же репозитория, собирается в один `index.html` и в образ не входит.
:::

### Части репозитория

<div class="tw">

| Часть | Что внутри | Сборка |
| --- | --- | --- |
| Сервер (`src/`, `migrations/`, `main.py`) | Сбор, обработка, публикация, бот, MCP-сервер | `Dockerfile` → образ `telebrief:<тег>`; деплой Coolify ([Эксплуатация](#ops-runbooks)) |
| Профили изданий (`data/city_profiles/`) | Места, районы, улицы, псевдонимы | В образ не входят, монтируются с хоста |
| Лендинг (`website/`) | Статический сайт продукта на Astro | `npm run build` в `website/` |
| Документация (`docs/`) | Эта документация: `docs/architecture/**/*.md` → `docs/index.html`; инструкции для оператора и рабочие документы | `npm run build` в `docs/`; исключена из образа (`.dockerignore`) |

</div>

### Стек

<div class="tw">

| Слой | Технология | Назначение |
| --- | --- | --- |
| Язык | Python 3.14 (`python:3.14.3-slim`) | Весь сервер; async на `asyncio` |
| Telegram-чтение | Telethon | Чтение чатов от имени пользовательской сессии (`sessions/user.session`) |
| Telegram-бот и канал | python-telegram-bot, Bot API | Команды владельца, публикация в канал, уведомления |
| Длинные статьи | Telegra.ph API | Страница статьи, ссылка на неё — в канал |
| LLM и эмбеддинги | `openai` SDK → OpenRouter | Gate, Rich Analysis, отбор, автор, редактор, эмбеддинги |
| База данных | PostgreSQL 18 + pgvector (`pgvector/pgvector:pg18`), psycopg 3, psycopg-pool | Все данные, векторы фрагментов и сюжетов |
| Очередь задач | Procrastinate (в той же PostgreSQL) | Сбор, обработка, Gate, публикация, обслуживание, периодические задачи |
| Браузер | Playwright (Chromium) | Сбор Facebook; сейчас выключен |
| MCP | `mcp`, uvicorn | MCP-сервер в процессе `telebrief-app` |
| Качество | pytest, Ruff, MyPy, pre-commit | Проверки и стиль кода (`pyproject.toml`, `Makefile`) |

</div>

### Карта исходного кода

<div class="tw">

| Путь | Что там |
| --- | --- |
| `main.py`, `src/core.py`, `src/bot_commands.py` | Процесс `telebrief-app`: бот, ручные выпуски, MCP |
| `src/worker.py`, `src/runtime.py`, `src/bootstrap.py` | Запуск воркеров Procrastinate с общим рантаймом |
| `src/jobs/` | Все задачи очереди и периодические расписания |
| `src/providers/`, `src/ingestion/` | Коллекторы источников, реестр, водяные знаки, исходы сбора |
| `src/processing/` | Фрагменты, эмбеддинги, кластеризация, Gate, Rich Analysis, хроника |
| `src/publication/` | Заявки, готовность, снимок, отбор, дайджест, статья, проверки, доставка |
| `src/domain/`, `src/repositories/`, `src/db/` | Доменные модели, слой доступа к данным, unit of work |
| `src/config/`, `config.yaml` | Схемы и парсинг настроек; редакционные параметры |
| `src/ai_providers.py`, `src/embedding_providers.py` | Провайдеры моделей и allowlist |
| `migrations/` | SQL-миграции (`scripts/migrate.py`, процесс `telebrief-migrate`) |
| `scripts/` | Превью выпусков на замороженных данных, сравнения, бэкапы, миграции |
| `tests/` | Модульные, интеграционные и PostgreSQL-тесты |

</div>

Модули `src/summarizer.py`, `src/grouper.py`, `src/editorial_*.py`, `src/collector.py` относятся к прежнему пути «по сообщениям» и остаются для совместимости и сравнений ([Риски](#l3-risks)). Канонический путь — Event-First.

### Модели

Генеративные модели задаются переменными окружения в `.env`, не в `config.yaml` (`AGENTS.md` §13.1):

<div class="tw">

| Настройка | Для чего |
| --- | --- |
| `OPENROUTER_MODEL` | Слоты публикации через запятую: основной `deepseek/deepseek-v4.1-flash-1017:floor`, резервные `openai/gpt-6-luna`, `anthropic/claude-haiku-5.5` |
| `OPENROUTER_PROCESSING_MODEL` | Фоновая обработка: Gate, Rich Analysis, хроника |
| `OPENROUTER_IMAGE_MODEL`, `GEMINI_IMAGE_MODEL` | Обложки статей; прямой Google-вариант — только если задан |
| `config.yaml` → `embedding` | Эмбеддинги фрагментов: `qwen/qwen3-embedding-8b:floor`, размерность 1536, таймаут 300 с |

</div>

`validate_model_allowed` (`src/ai_providers.py`) отклоняет генеративную модель, не объявленную в переменных `ENV_MODEL_VARS`, до сетевого запроса; суффикс вроде `:floor` при сравнении отбрасывается. Модель эмбеддингов этой проверкой не покрыта, а при пустом списке переменных проверка ничего не ограничивает ([Риски](#l3-risks)). Значения в `.env.example` — пример и могут отличаться от рабочих.

### Где настройки

Значения меняются в `config.yaml` (монтируется в контейнеры только для чтения) и `.env`; комментарии в `config.yaml` объясняют выбор значений. Документ называет ключ там, где описано его действие:

<div class="tw">

| Ключ `config.yaml` | Что задаёт | Описано |
| --- | --- | --- |
| `channels` | Источники издания | [Сбор и обработка](#l3-ingestion-detail) |
| `settings.schedule_time`, `timezone`, `pre_publish_lead_minutes`, `publication_*_minutes`, `periodic_ingestion_enabled` | Расписание дайджеста, окна заявки, фоновый сбор | [Заявка на выпуск](#l3-readiness-detail) |
| `settings.event_pipeline`, `settings.edition_scopes`, `embedding` | Фрагменты, кластеризация, Gate, география издания, эмбеддинги | [Сбор и обработка](#l3-ingestion-detail) |
| `settings.publication_editorial` | Режим дайджеста, лимиты автора и редактора, статья, хроника | [Дайджест](#l3-digest-detail), [Статья](#l3-article-detail), [Хроника](#l3-period-review-detail) |
| `settings.digest_rubrics`, `include_statistics` | Рубрики и статистика дайджеста | [Дайджест](#l3-digest-detail) |
| `settings.article`, `weekly_article`, `monthly_article`, `quarterly_article`, `half_year_article`, `yearly_article` | Расписание, окна и дедлайны статей и обзоров | [Статья](#l3-article-detail), [Хроника](#l3-period-review-detail) |
| `settings.target_chat_id`, `target_user_id`, `admin_user_ids` (не задан — берётся `target_user_id`) | Куда публиковать, кто управляет ботом и получает уведомления | [Заявка на выпуск](#l3-readiness-detail) |
| `facebook` | Сбор из Facebook (выключен) | — |

</div>

### Другая документация в репозитории

<div class="tw">

| Документ | О чём |
| --- | --- |
| `AGENTS.md` | Редакционный контракт продукта (§0) и инженерные правила — первоисточник для правил выпуска |
| `docs/operations/production.md` | Деплой, доступ к серверу, бэкап, откат |
| `docs/operations/server-migration.md` | Переезд и подготовка сервера |
| `docs/operations/oracle-cloud-budget.md` | Бюджет OCI и Free Tier |
| `docs/operations/database-backup.md` | Резервное копирование базы |
| `docs/adr/0001-extensibility-architecture.md` | Ранний ADR о расширяемости |

</div>

### Implementation References:

- **Telebrief:** `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `pyproject.toml`, `Makefile`, `.env.example`, `src/ai_providers.py` (`validate_model_allowed`).

[↑ Основные компоненты (Уровень 1)](#l1-components)
