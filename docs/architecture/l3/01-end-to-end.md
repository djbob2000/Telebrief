---
id: "l3-end-to-end"
level: "L3"
eyebrow: "Уровень 3 · Сквозной процесс"
nav: "Сквозная диаграмма"
group: "Сквозной процесс"
---

## Сквозная диаграмма планового дайджеста

Процесс простыми словами — в [карте процессов](#l2-interaction-map). Здесь те же шаги ①–⑪ на уровне задач очереди Procrastinate, процессов Docker Compose и таблиц PostgreSQL.

::: tldr
**TL;DR для разработчика.** Всю оркестрацию держит Procrastinate: периодическая задача `publication_schedule_dispatcher` раз в минуту создаёт и сверяет заявки (`publication_refresh_runs`). Сбор, обработка, фильтр, отбор, генерация и доставка — отдельные задачи в очередях `collection`, `processing`, `authority`, `publication`, которые ставятся в той же транзакции, что и данные. Процесс `telebrief-app` с ботом сам ничего не генерирует — он лишь создаёт заявку.
:::

```mermaid
sequenceDiagram
    participant Disp as ⏱️ Планировщик · publication_schedule_dispatcher
    participant Orch as 🧭 PublicationOrchestrator
    participant Scan as 📥 scan_source · collection
    participant TG as 💬 Telegram · Telethon и Bot API
    participant Proc as 🧠 processing-задачи · processing
    participant Auth as 🚦 Gate-задачи · authority
    participant Pub as 📰 publication-задачи · publication
    participant LLM as ✨ OpenRouter · LLM и эмбеддинги
    participant DB as 🗄️ PostgreSQL
    Note over Disp: каждую минуту, слот 09:00 Europe/Kyiv
    Disp->>Orch: ① 08:30 request(trigger=scheduled, request_key)
    Orch->>DB: INSERT publication_refresh_runs и publication_refresh_sources
    Orch->>Scan: defer scan_source(source_id, PRE_PUBLISH)
    Scan->>TG: ② окно 24 ч и догрузка от водяного знака
    TG-->>Scan: сообщения
    Scan->>DB: source_items, source_item_revisions, collection_runs
    Scan->>Proc: defer process_event_revisions(ids) в той же транзакции
    Proc->>DB: ③ source_fragments
    Proc->>LLM: эмбеддинги пачками
    Proc->>DB: source_fragment_embeddings, fragment_embedding_vectors
    Proc->>DB: ④ story_fragments, story_cluster_state, stories
    Disp->>Orch: reconcile каждую минуту
    Orch->>DB: все источники succeeded и ревизии обработаны?
    Orch->>Auth: defer process_publication_authority_gap при пробелах Gate
    Auth->>LLM: ⑤ Gate-пачка до 10 сюжетов с CHAT TIMELINE
    Auth->>DB: story_event_triage_decisions, story_edition_scope_decisions
    Auth->>DB: brief_payload, story_revisions
    Proc->>LLM: ⑥ process_event_enrichment для enrichment=ANALYZE
    Proc->>DB: story_revisions.event_payload
    Orch->>DB: ⑦ status=ready_for_preparation, knowledge_snapshot_at
    Orch->>Pub: defer prepare_publication_from_intent
    Pub->>DB: publication_runs, publication_candidates (seal)
    Pub->>LLM: select_stories_for_publication
    Pub->>DB: publication_selection_decisions, publication_inputs
    Pub->>LLM: ⑧ generate_publication: автор дайджеста
    Pub->>Pub: ⑨ assess_digest_candidate
    Pub->>LLM: DigestEditor до 3 вызовов
    Pub->>DB: publications, publication_generation_attempts
    Pub->>DB: prepare_delivery_payloads
    Pub->>TG: ⑩ deliver_publication_payload в канал через Bot API
    alt генерация отклонена
        Pub->>Pub: retry_digest_publication через 20 мин
        Pub->>DB: ⑪ publication_failure_notifications
    end
```

### Процессы и очереди

<div class="tw">

| Процесс Compose | Команда | Очереди | Параллельность |
| --- | --- | --- | --- |
| `telebrief-app` | `python main.py` | — (бот, MCP-сервер, создание заявок) | — |
| `telebrief-worker` | `python -m src.worker` | `publication`, `maintenance` | 2 |
| `telebrief-processing-worker` | `python -m src.worker` | `processing`, `collection`, `enrichment`, `default` | 1 |
| `telebrief-authority-worker` | `python -m src.worker` | `authority` | 2 |
| `telebrief-migrate` | `python scripts/migrate.py` | — (один раз перед стартом) | — |
| `postgres` | `pgvector/pgvector:pg18` | — | `max_connections=50` |

</div>

`validate_worker_configuration` запрещает больше 2 параллельных задач для очереди `processing` (пока обновления кластеров не сериализованы полностью) и больше 4 для `authority` (иначе исчерпываются соединения БД и лимиты провайдера). Голый `procrastinate worker` запускать нельзя: он не устанавливает рантайм (`src/runtime.py`), без которого падает любая задача.

### Периодические задачи

<div class="tw">

| Задача | Расписание (UTC) | Что делает |
| --- | --- | --- |
| `publication_schedule_dispatcher` | каждую минуту | Сверяет открытые заявки, создаёт новые по расписанию |
| `dispatch_due_sources` | каждую минуту | Плановый опрос источников; выходит сразу, пока `periodic_ingestion_enabled: false` |
| `requeue_unfinished_revisions` | каждые 10 минут | Подбирает застрявшие ревизии, до 500 за проход |
| `retry_stalled_jobs` | каждые 10 минут | Возвращает в очередь задачи, зависшие в состоянии `doing` |
| `periodic_event_enrichment_dispatch` | каждые 5 минут | Ставит Rich Analysis для сюжетов с `enrichment=ANALYZE`, до 40 за цикл |
| `periodic_background_authority_dispatch` | в 0 минут каждого 5-го часа | Фоновый Gate для накопившихся сюжетов |
| `publication-failure-notification-redrive` | каждые 5 минут | Повторяет неотправленные уведомления о сбое |
| `storyline-chronicle` | в 35 минут каждого 2-го часа | Хроника сюжетных линий для итогов периода |
| `situation-memory-update` | 04:20 и 16:20 | Память о текущих ситуациях для дайджеста |
| `retention-cleanup` | 03:15 | Очистка рабочих векторов, истории задач и наблюдений сбора |
| `openrouter-balance-check` | 07:00 | Предупреждение владельцам о низком балансе OpenRouter |
| `facebook-deep-sweep` | каждые 6 часов | Глубокий обход Facebook; без включённого Facebook не работает |

</div>

### Implementation References:

- **Telebrief:** `docker-compose.yml` (процессы и очереди), `src/worker.py` (`WORKER_QUEUES`, `validate_worker_configuration`), `src/jobs/app.py` (приложение Procrastinate).
- **Telebrief:** `src/jobs/schedules.py`, `src/jobs/ingestion.py`, `src/jobs/event_processing.py`, `src/jobs/event_authority.py`, `src/jobs/event_enrichment.py`, `src/jobs/publication.py`, `src/jobs/maintenance.py`, `src/jobs/facebook.py`.

[↑ Общая карта процессов (Уровень 2)](#l2-interaction-map)
