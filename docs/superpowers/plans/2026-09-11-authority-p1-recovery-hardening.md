# Authority P1 Recovery Hardening Implementation Plan

> **Для реализации:** делать TDD по каждому task: RED → минимальный GREEN → полный regression run. Не смешивать функциональные изменения двух P1 в один непроверяемый коммит.

**Goal:** устранить два production P1:
1. self-locking recovery у `process_background_authority_batch`;
2. terminal exhaustion assignment’ов из-за partial Gate V2 response.

**Base:** `dev@c8c5db4c9b462165bcb10257cd7e9a72e707012c`.

**Предлагаемый файл плана в repo:**  
`docs/superpowers/plans/2026-09-11-authority-p1-recovery-hardening.md`

## Архитектурные ограничения

- `process_background_authority_batch` сохраняет execution lock `authority-background:{edition_id}`.
- Новый background job **не должен** получать `queueing_lock=authority-background:{edition_id}`.
- `dispatch_background_authority` остаётся единственной точкой dedupe через `authority-background-dispatch:{edition_id}`.
- Stalled recovery не должен полагаться на abort мёртвого worker.
- Partial Gate response не является успешным полным batch.
- Уже валидные Gate decisions из partial response сохраняются и не вызываются повторно.
- Missing assignment сначала получает bounded in-flight retry.
- Durable `attempt_count` нельзя увеличивать assignment’у, который ещё не получил собственный isolated retry.
- Все provider calls остаются вне DB transaction.
- Stage claim heartbeat работает всё время, пока выполняются initial + recovery calls.
- Edition coordination lease не держится во время AI.
- Все retries ограничены существующим `triage_split_max_extra_calls_per_cycle`.
- Общий provider wall-clock budget остаётся ограниченным `authority_provider_timeout_seconds`.
- Production-safe `triage_batch_size = 10` должен быть зафиксирован в кодовой конфигурации, а не только ручным edit на сервере.
- В этом patch **не нужна DB migration**.

---

## Task 1 — Убрать первопричину Procrastinate lock-cycle

**Files**
- Modify: `src/jobs/event_authority.py`
- Test: `tests/jobs/test_event_authority.py`

Сейчас task уже имеет execution lock:

```python
@procrastinate_app.task(
    queue="processing",
    name="process_background_authority_batch",
    lock="authority-background:{edition_id}",
)
```

но dispatcher дополнительно выдаёт тому же job:

```python
queueing_lock=f"authority-background:{edition_id}"
```

Именно это позволяет получить наблюдавшийся цикл `stalled doing → queued successor owns queueing_lock → stalled cannot be retried`. 

### Step 1.1 — RED test

Добавить в `tests/jobs/test_event_authority.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_dispatch_does_not_queueing_lock_execution_locked_batch(
    monkeypatch,
):
    configured = MagicMock()
    configured.defer_async = AsyncMock()
    configure = MagicMock(return_value=configured)

    monkeypatch.setattr(
        authority_jobs,
        "_load_background_targets",
        AsyncMock(return_value=[SimpleNamespace(story_id=1)]),
    )
    monkeypatch.setattr(
        authority_jobs.process_background_authority_batch,
        "configure",
        configure,
    )

    await authority_jobs.dispatch_background_authority(edition_id=1)

    configure.assert_called_once_with(
        priority=authority_jobs.BACKGROUND_AUTHORITY_PRIORITY,
    )
    configured.defer_async.assert_awaited_once_with(edition_id=1)
```

Run:

```bash
pytest -q \
  tests/jobs/test_event_authority.py::test_background_dispatch_does_not_queueing_lock_execution_locked_batch
```

**Expected RED:** `configure()` фактически получает ещё `queueing_lock="authority-background:1"`.

### Step 1.2 — Minimal implementation

В `dispatch_background_authority()` заменить:

```python
try:
    await process_background_authority_batch.configure(
        priority=BACKGROUND_AUTHORITY_PRIORITY,
        queueing_lock=f"authority-background:{edition_id}",
    ).defer_async(edition_id=edition_id)
except AlreadyEnqueued:
    return
```

на:

```python
await process_background_authority_batch.configure(
    priority=BACKGROUND_AUTHORITY_PRIORITY,
).defer_async(edition_id=edition_id)
```

Локальный `AlreadyEnqueued` import в этой функции после этого удалить.

**Не удалять execution lock task decorator.**

### Step 1.3 — GREEN

Запустить новый тест и целиком:

```bash
pytest -q tests/jobs/test_event_authority.py
```

**Acceptance:** новый `process_background_authority_batch` имеет `lock`, но `queueing_lock IS NULL`.

---

## Task 2 — Сделать stalled recovery безопасным для уже существующих lock-cycle

**Files**
- Modify: `src/jobs/maintenance.py`
- Test: `tests/jobs/test_maintenance.py`

Текущий код ловит `UniqueViolation` и просто `continue`, поэтому конфликт может жить бесконечно. 

### Важный контракт

Не делать:

```python
cancel_job_by_id_async(stalled.id, abort=True)
```

для stale `doing`.

Abort running job требует участия worker, исполняющего этот job. Если worker уже умер, это не гарантирует снятия `doing`/execution lock.

Вместо этого повторить автоматически уже доказанный production recovery:

`find todo blocker → cancel todo blocker → retry stalled`.

`JobManager.list_jobs_async()` официально умеет фильтровать по `status` и `queueing_lock`; `cancel_job_by_id_async()` штатно отменяет ожидающий job.

### Step 2.1 — заменить старый тест

Текущий:

```python
test_retry_stalled_jobs_ignores_existing_queueing_lock
```

должен стать RED regression:

```python
@pytest.mark.asyncio
async def test_retry_stalled_jobs_cancels_same_lock_successor_then_retries():
    stalled = SimpleNamespace(
        id=66832,
        task_name="process_background_authority_batch",
        lock="authority-background:1",
        queueing_lock="authority-background:1",
    )
    successor = SimpleNamespace(
        id=66839,
        task_name="process_background_authority_batch",
        lock="authority-background:1",
        queueing_lock="authority-background:1",
        status="todo",
    )

    violation = procrastinate.exceptions.UniqueViolation(
        constraint_name="procrastinate_jobs_queueing_lock_idx_v1",
        queueing_lock="authority-background:1",
    )

    fake_app = MagicMock()
    fake_app.job_manager.get_stalled_jobs = AsyncMock(return_value=[stalled])
    fake_app.job_manager.retry_job = AsyncMock(
        side_effect=[violation, None]
    )
    fake_app.job_manager.list_jobs_async = AsyncMock(
        return_value=[successor]
    )
    fake_app.job_manager.cancel_job_by_id_async = AsyncMock()

    with patch("src.jobs.maintenance.procrastinate_app", fake_app):
        await retry_stalled_jobs(context=SimpleNamespace(), timestamp=0)

    fake_app.job_manager.list_jobs_async.assert_awaited_once_with(
        status="todo",
        queueing_lock="authority-background:1",
    )
    fake_app.job_manager.cancel_job_by_id_async.assert_awaited_once_with(66839)
    assert fake_app.job_manager.retry_job.await_count == 2
```

RED: current implementation не вызывает `list_jobs_async`/`cancel_job_by_id_async`.

### Step 2.2 — safety regression

Добавить тест, что maintenance **не отменяет произвольный чужой job**:

```python
@pytest.mark.asyncio
async def test_retry_stalled_jobs_does_not_cancel_unrelated_queueing_lock_owner():
    stalled = SimpleNamespace(
        id=1,
        task_name="task_a",
        lock="lock-a",
        queueing_lock="shared",
    )
    blocker = SimpleNamespace(
        id=2,
        task_name="task_b",
        lock="lock-b",
        queueing_lock="shared",
        status="todo",
    )

    # retry_job -> queueing-lock UniqueViolation
    # list_jobs_async -> [blocker]

    # Expected:
    # cancel_job_by_id_async НЕ вызывается
    # повторного retry_job НЕ происходит
```

### Step 2.3 — implementation

При `UniqueViolation` с queueing lock:

```python
blockers = list(
    await procrastinate_app.job_manager.list_jobs_async(
        status="todo",
        queueing_lock=exc.queueing_lock,
    )
)

recoverable = [
    blocker
    for blocker in blockers
    if blocker.id != job.id
    and blocker.task_name == job.task_name
    and blocker.lock == job.lock
    and job.lock == exc.queueing_lock
]
```

Только при `len(recoverable) == 1`:

```python
blocker = recoverable[0]
await procrastinate_app.job_manager.cancel_job_by_id_async(blocker.id)

try:
    await procrastinate_app.job_manager.retry_job(job)
except procrastinate.exceptions.UniqueViolation:
    logger.error(
        "stalled job %s still cannot be retried after cancelling blocker %s",
        job.id,
        blocker.id,
    )
```

Если exact-match blocker не найден — оставить job как есть и логировать `warning/error`, но **не угадывать и не отменять чужие jobs**.

### Step 2.4 — GREEN

```bash
pytest -q tests/jobs/test_maintenance.py
```

---

## Task 3 — Сделать `triage_batch_size=10` кодовым safety invariant

**Files**
- Modify: `src/config/schemas/publication.py`
- Modify: `config.yaml`
- Modify: `config.yaml.example`
- Test: `tests/config/test_config_schemas.py`

Сейчас schema default остаётся `25`, а верхняя граница вообще не валидируется; одновременно уже существует отдельный bounded budget `triage_split_max_extra_calls_per_cycle=8`. 

Production показал конфигурацию `80`, после чего пришлось вручную снижать её до `10`. 

### Step 3.1 — RED

```python
def test_event_pipeline_uses_safe_gate_batch_default():
    assert EventPipelineConfig().triage_batch_size == 10


@pytest.mark.parametrize("value", [0, 11, 80])
def test_event_pipeline_rejects_unsafe_gate_batch_size(value):
    with pytest.raises(ValueError, match="triage_batch_size"):
        EventPipelineConfig(triage_batch_size=value)
```

### Step 3.2 — implementation

В `EventPipelineConfig`:

```python
triage_batch_size: int = 10
```

В `__post_init__`:

```python
if not 1 <= self.triage_batch_size <= 10:
    raise ValueError("triage_batch_size must be between 1 and 10")
```

И явно:

```yaml
triage_batch_size: 10
```

в `config.yaml` и `config.yaml.example`.

Важно: `10` **снижает вероятность** truncation, но не является доказательством полноты ответа. Поэтому Task 4–5 всё равно обязательны.

---

## Task 4 — Явно классифицировать partial Gate V2 response

**Files**
- Modify: `src/processing/event_triage.py`
- Test: `tests/processing/test_event_triage.py`

Текущий parser строит `items_by_id`, а отсутствующий `story_id` просто добавляет в `deferred_ids`. Поэтому наверх возвращается deferred story без причины, а authority layer позже превращает её в `"other"`. 

### Новый контракт `StoryGateBatchResult`

Добавить только одно новое поле:

```python
@dataclass(frozen=True)
class StoryGateBatchResult:
    results: tuple[StoryGateResult, ...]
    deferred_story_ids: tuple[int, ...]
    batch_error_kind: str | None = None
    prompt_hash: str | None = None
    fence_lost_story_ids: tuple[int, ...] = ()
    missing_story_ids: tuple[int, ...] = ()
```

Не добавлять отдельный `partial: bool`: это дублирующее состояние.

### Step 4.1 — вычислять missing IDs

Сразу после построения `items_by_id`:

```python
expected_story_ids = {story.story_id for story in uncached_stories}
returned_story_ids = set(items_by_id) & expected_story_ids

missing_story_ids = tuple(
    story.story_id
    for story in uncached_stories
    if story.story_id not in returned_story_ids
)
```

Unknown story IDs модели не считать coverage.

### Step 4.2 — вернуть explicit error kind

В нормальном return:

```python
return StoryGateBatchResult(
    results=tuple(final_results),
    deferred_story_ids=tuple(deferred_ids),
    batch_error_kind=(
        "partial_response" if missing_story_ids else None
    ),
    prompt_hash=prompt_hash,
    fence_lost_story_ids=tuple(sorted(fence_lost_ids)),
    missing_story_ids=missing_story_ids,
)
```

Provider exception path остаётся со своим `classify_provider_failure(exc)` и `missing_story_ids=()`.

### Step 4.3 — audit run не должен лгать `succeeded`

Для ответа, где есть `missing_story_ids`, запись `story_event_triage_runs` должна получить:

```text
status = failed
error_kind = partial_response
```

При этом валидные returned decisions **сохраняются**.

DB migration для `valid_count/deferred_count` в этом P1 patch не делать. Вместо этого добавить structured log:

```python
logger.warning(
    "event_first_gate_partial_response",
    extra={
        "requested_count": len(uncached_stories),
        "valid_count": len(new_valid_results),
        "deferred_count": len(deferred_ids),
        "missing_count": len(missing_story_ids),
        "missing_story_ids": missing_story_ids,
        "prompt_hash": prompt_hash,
    },
)
```

Если потом нужна долговременная аналитика counts — отдельная migration после incident fix.

### Step 4.4 — RED/GREEN postgres regression

Новый тест должен создать 2 uncached stories, а AI вернуть только первую.

Проверки:

```python
assert [r.story_id for r in result.results] == [story_1]
assert result.deferred_story_ids == (story_2,)
assert result.missing_story_ids == (story_2,)
assert result.batch_error_kind == "partial_response"
```

И DB:

```sql
SELECT status, error_kind, story_count
FROM story_event_triage_runs
ORDER BY id DESC
LIMIT 1;
```

Expected:

```python
("failed", "partial_response", 2)
```

Плюс decision существует для `story_1` и отсутствует для `story_2`.

---

## Task 5 — In-flight isolated recovery до durable retry budget

**Files**
- Modify: `src/processing/event_authority.py`
- Test: `tests/processing/test_event_authority.py`

Это главный semantic fix.

Сейчас любой `deferred` проходит через:

```python
result.batch_error_kind or "other"
```

и немедленно получает `record_failure()`. 

Новый порядок:

```text
initial batch
    ↓
valid results → accepted
    ↓
missing_story_ids
    ↓
bounded singleton recovery
    ├─ success → accepted, retry state cleared
    ├─ isolated failure → durable attempt_count += 1
    └─ recovery budget exhausted before attempt
           → leave pending; DO NOT increment durable attempt
```

### Step 5.1 — не выполнять retry внутри `StoryTriageService`

`StoryTriageService` отвечает за:
- prompt;
- parse;
- validation;
- persistence valid decisions;
- report partial response.

`EventAuthorityService` отвечает за:
- claims;
- heartbeat;
- bounded retries;
- durable retry accounting.

Не смешивать эти responsibilities.

### Step 5.2 — initial result bookkeeping

После initial Gate call построить:

```python
result_by_id = {gate.story_id: gate for gate in result.results}
fence_lost = set(result.fence_lost_story_ids)

deferred = set(result.deferred_story_ids) - fence_lost
missing = set(result.missing_story_ids) - fence_lost
```

Для `deferred - missing` сохранить текущую durable failure семантику (`other` либо фактический provider kind).

Для `missing` **ещё не вызывать `record_failure()`**.

### Step 5.3 — bounded singleton recovery

Использовать уже существующий:

```python
cfg.triage_split_max_extra_calls_per_cycle
```

как максимальное число дополнительных provider calls.

Идти в deterministic claimed order:

```python
partial_targets = [
    item for item in claimed
    if item.target.story_id in missing
]
```

Для каждого, пока budget > 0, делать Gate только на:

```python
[item.state]
```

с exact:

```python
assignment_id_by_story={
    item.target.story_id: item.target.assignment_id
}
```

и теми же:
- `edition_id`;
- `scope_config`;
- `scope_hash`;
- `source_cutoff_at`;
- `decision_fence`;
- `before_decision_persist`.

### Step 5.4 — heartbeat остаётся активным

Не завершать `heartbeat_task` после initial batch.

Он должен охватывать:

```text
initial AI call
+ all bounded singleton recovery calls
```

Stage claim должен
