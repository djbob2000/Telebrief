---
id: "l3-maintenance-detail"
level: "L3"
eyebrow: "Уровень 3 · Компонент"
nav: "Обслуживание и хранение"
group: "Платформа"
---

## Обслуживание и хранение данных

Процесс простыми словами — в [Сценарии 5](#l2-maintenance). Расписание всех периодических задач — в [Сквозной диаграмме](#l3-end-to-end); хроника — в [отдельном разделе](#l3-period-review-detail).

::: tldr
**TL;DR для разработчика.** Задачи очереди `maintenance` в `src/jobs/maintenance.py`, каждая с `queueing_lock`, выполняет процесс `telebrief-worker`. Память о ситуациях (`update_situation_memory`) — неизменяемые снимки в `edition_situation_memory_snapshots`, только фон для дайджеста. Очистка (`RetentionService.cleanup`) обнуляет векторы, но не удаляет строки знаний, и перед этим копирует центроиды опубликованных сюжетов в `story_semantic_vectors`.
:::

### Память о текущих ситуациях

- **Trigger:** `situation-memory-update`, cron `20 4,16 * * *` (UTC), для всех изданий.
- **Input:** текущий снимок памяти и сюжеты, изменённые после него: до `MAX_INPUT_STORIES = 220` и `MAX_INPUT_CHARS = 110 000` символов. Первый запуск заполняет память за `BOOTSTRAP_DAYS = 30` дней кусками по `BOOTSTRAP_CHUNK_DAYS = 3`.
- **Processing:** модель возвращает операции `ADD` / `UPDATE` / `RESOLVE` над текущей памятью, а не переписывает её. Каждое утверждение ссылается на фрагменты; числа и даты должны встречаться в них, иначе поле отбрасывается (`grounding.py`). Статусы ситуации: `ongoing`, `improving`, `worsening`, `resolved`.
- **Output:** новый неизменяемый снимок `edition_situation_memory_snapshots` со ссылкой на предыдущий; дайджест записывает id использованного снимка в `digest_implementation_versions.situation_memory_snapshot_id`.
- **Constraints:** до `MAX_SITUATIONS = 10` ситуаций; ситуация без подтверждения `STALE_AFTER_DAYS = 21` дня устаревает; до `MAX_REFS_PER_SITUATION = 40` ссылок.
- **Failure:** ошибка загрузки памяти в генерации дайджеста логируется, и дайджест генерируется без фона (`_attach_situation_memory`).
- **Модель:** задача передаёт `settings.openai_model` (без настройки — `gpt-5-nano`) провайдеру статей; попадает ли эта модель в allowlist и какая модель отвечает на деле — **Needs verification** ([Риски](#l3-risks)).

### Очистка рабочих данных

`retention-cleanup`, cron `15 3 * * *` (UTC), `RetentionService.cleanup`:

<div class="tw">

| Шаг | Что делает | Срок | Политика |
| --- | --- | --- | --- |
| Синхронизация смысловых векторов | Копирует центроид опубликованного сюжета в `story_semantic_vectors` (половинная точность) до его вытеснения | — | `sync_story_semantic_vectors` |
| Центроиды сюжетов | Обнуляет вектор в `story_cluster_state` | 14 дней тишины сюжета | `RetentionPolicy.centroid_days` |
| Векторы фрагментов | Обнуляет вектор в `fragment_embedding_vectors`; строка и связи остаются, тот же текст позже заполнит его снова | 14 дней | `RetentionPolicy.fragment_vector_days` |
| История задач | Удаляет завершённые задачи Procrastinate (не периодические отметки) | 720 ч | `RetentionPolicy.job_history_hours` |
| Наблюдения сбора | Удаляет `collection_run_revision_observations` старых прогонов, не нужных свежим заявкам | 30 дней | `RetentionPolicy.collection_observation_days` |
| Артефакты Facebook | Удаляет файлы с истёкшим сроком, строку помечает удалённой | по сроку артефакта | `_cleanup_artifacts` |

</div>

Каждый шаг идёт пачками по 2000 строк (`batch_size`), каждая в своей транзакции, с паузой 0,2 с и не больше 500 пачек на шаг. Тексты сообщений, сюжеты, ревизии, решения Gate, хроника и публикации не удаляются никогда ([ADR-0008](#adr-0008)). Обложки статей в `data/articles` очистка не трогает.

### Самовосстановление и предупреждения

<div class="tw">

| Задача | Расписание (UTC) | Что делает |
| --- | --- | --- |
| `retry_stalled_jobs` | каждые 10 минут | `get_stalled_jobs` Procrastinate → `retry_job`. Если `queueing_lock` занят зависшей копией той же задачи — отменяет её и повторяет |
| `requeue_unfinished_revisions` | каждые 10 минут | Недообработанные ревизии — [Сбор и обработка знаний](#l3-ingestion-detail) |
| `publication-failure-notification-redrive` | каждые 5 минут | Повторяет неотправленные `publication_failure_notifications` |
| `openrouter-balance-check` | 07:00 | Предупреждает всех `admin_user_ids`, если на счёте меньше `MIN_CREDITS_REMAINING_USD = 5` долларов или от дневного лимита осталось меньше `MIN_DAILY_BUDGET_REMAINING_USD = 0,5`; без ключа или получателей не работает |

</div>

### Implementation References:

- **Telebrief:** `src/jobs/maintenance.py`, `src/publication/situation_memory.py`, `src/publication/grounding.py`, `src/retention.py` (`RetentionService`, `RetentionPolicy`), `src/openrouter_balance.py`, `src/publication/notifications.py`.
- **Telebrief:** миграции `0040_edition_situation_memory.sql`, `0041_vector_retention.sql`.

[↑ Сценарий 5 (Уровень 2)](#l2-maintenance)
