# Бердянск: выбор форумных тем из источника — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить поддержку выборочного сбора сообщений из форумных тем Telegram и оставить для `@Berdyansk_drb` только темы «Проблемы ЖКХ» (`235525`) и «Новости Бердянска» (`43339`).

**Architecture:** `ChannelConfig` получит необязательный список `ForumTopicConfig`. Источник без тем продолжит собираться через текущий `iter_messages`, а источник с темами будет один раз разрешён в Telegram и для каждой темы будет запрашиваться через `messages.SearchRequest(top_msg_id=...)`. Каждая тема станет отдельным логическим входом суммаризатора, чтобы AI не смешивал ЖКХ и городские новости.

**Tech Stack:** Python 3.14, PyYAML, Telethon, pytest, Docker Compose, Oracle Linux 9.

## Global Constraints

- Не менять поведение существующих источников без `topics`.
- Не читать остальные темы `@Berdyansk_drb` и не передавать их модели.
- Использовать Telegram topic IDs `235525` и `43339`.
- Сохранить русские названия тем и ссылки на исходные Telegram-сообщения.
- Не добавлять новые внешние зависимости.
- Не смешивать в коммит implementation-файлы с уже существующими незакоммиченными изменениями пользователя.

---

### Task 1: Модель конфигурации и YAML

**Files:**
- Modify: `src/config_loader.py` — добавить `ForumTopicConfig` и поле `ChannelConfig.topics`.
- Modify: `config.yaml` — заменить полный источник `@Berdyansk_drb` на две выбранные темы.
- Modify: `config.yaml.example` — показать синтаксис `topics`.
- Test: `tests/test_config_loader.py` — загрузка и значения по умолчанию.

**Interfaces:**
- `ForumTopicConfig(id: int, name: str)` — идентификатор и отображаемое название форумной темы.
- `ChannelConfig.topics: list[ForumTopicConfig]` — пустой список означает сбор всего источника по старому пути.

- [ ] **Step 1: Write failing tests**

Добавить тесты, которые загружают YAML с:

```yaml
channels:
  - id: "@Berdyansk_drb"
    name: "Бердянск Свежие Объявления"
    topics:
      - id: 235525
        name: "Проблемы ЖКХ"
      - id: 43339
        name: "Новости Бердянска"
settings:
  target_user_id: 123456789
```

и проверяют два `ForumTopicConfig`, а также проверяют, что у обычного источника `topics == []`.

- [ ] **Step 2: Run tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_config_loader.py -k 'topic'`

Expected: FAIL because `ChannelConfig` and YAML parsing do not yet expose `topics`.

- [ ] **Step 3: Implement minimal config support**

Добавить dataclass и парсинг с проверками типов:

```python
@dataclass
class ForumTopicConfig:
    id: int
    name: str

@dataclass
class ChannelConfig:
    ...
    topics: list[ForumTopicConfig] = field(default_factory=list)
```

Парсер должен принимать только целочисленный `id` и непустое строковое `name`; отсутствующий `topics` и `topics: []` должны давать пустой список.

- [ ] **Step 4: Update production and example configuration**

В `config.yaml` использовать ровно две темы:

```yaml
  - id: "@Berdyansk_drb"
    name: "Бердянск Свежие Объявления"
    topics:
      - id: 235525
        name: "Проблемы ЖКХ"
      - id: 43339
        name: "Новости Бердянска"
    prompt_extra: *regional_prompt
```

Удалить отдельную запись, которая читала весь этот источник.

- [ ] **Step 5: Run config tests**

Run: `./.venv/bin/pytest -q tests/test_config_loader.py -k 'topic or channel'`

Expected: all selected tests pass.

---

### Task 2: Topic-scoped Telegram collection

**Files:**
- Modify: `src/collector.py` — добавить topic-scoped request и раздельные логические результаты.
- Test: `tests/test_collector.py` — создать тесты для topic request, обычного источника и ошибки одной темы.

**Interfaces:**
- `MessageCollector._fetch_topic_messages(entity, channel_config, topic, lookback_time) -> list[Message]` — получает только одну тему через `messages.SearchRequest(..., top_msg_id=topic.id)`.
- `MessageCollector.fetch_messages(...) -> dict[str, list[Message]]` — для topic source возвращает ключи `"<channel> — <topic>"`.

- [ ] **Step 1: Write failing tests**

Добавить async-тесты с mock Telegram client:

1. Для тем `235525` и `43339` проверить два вызова `SearchRequest`, у которых `top_msg_id` равен соответствующему ID, и два ключа результата с названиями тем.
2. Для источника без тем проверить, что используется текущий `iter_messages` и результат остаётся одним ключом.
3. При исключении для одной темы проверить, что вторая тема всё равно возвращается, а ошибка логируется.

- [ ] **Step 2: Run tests to verify failure**

Run: `./.venv/bin/pytest -q tests/test_collector.py -k 'topic'`

Expected: FAIL because topic-specific collection does not exist.

- [ ] **Step 3: Implement topic request**

Использовать `functions.messages.SearchRequest` с `types.InputMessagesFilterEmpty`, `q=""`, временным окном и `top_msg_id=topic.id`. Общую обработку преобразования Telegram `Message` в проектный `Message` вынести в небольшой helper, чтобы обычный и topic пути не расходились по ссылкам, media и sender.

- [ ] **Step 4: Preserve failure isolation**

В `fetch_messages` разрешать entity один раз для channel с темами, собирать темы независимо и продолжать после ошибки отдельной темы. Для обычных источников оставить существующее исключение/повторное подключение без изменений.

- [ ] **Step 5: Run collector tests**

Run: `./.venv/bin/pytest -q tests/test_collector.py`

Expected: all collector tests pass.

---

### Task 3: Integration and regression coverage

**Files:**
- Modify: `src/core.py` only if URL/source-name handling needs an explicit topic-aware adjustment.
- Modify: `README.md` — document `topics` configuration and the two production topics.
- Test: `tests/test_core.py` and `tests/test_config_loader.py` only where integration behavior requires it.

- [ ] **Step 1: Verify logical topic names reach summarization**

Проверить, что ключи `Бердянск Свежие Объявления — Проблемы ЖКХ` и `... — Новости Бердянска` становятся отдельными `channel_summaries`, а `channel_urls` сохраняет публичную ссылку на сообщение.

- [ ] **Step 2: Add documentation**

Добавить в README пример `topics` и пояснить, что без `topics` источник читается целиком, а с `topics` — только выбранные форумные темы.

- [ ] **Step 3: Run complete local verification**

Run: `git diff --check && ./.venv/bin/pytest -q`

Expected: full suite passes with coverage threshold satisfied.

---

### Task 4: Deploy and verify production

**Files:**
- Deploy: `config.yaml`, `src/config_loader.py`, `src/collector.py`, `README.md` and any integration test changes to `/home/opc/Telebrief`.

- [ ] **Step 1: Build the production image**

Run on Oracle: `sudo /usr/local/bin/docker build -t telebrief:local .`

- [ ] **Step 2: Recreate the service**

Run on Oracle: `sudo /usr/local/bin/docker compose up -d --force-recreate telebrief`

- [ ] **Step 3: Verify logs and health**

Check `docker ps` for `telebrief ... (healthy)` and logs for:

```text
Configured channels: 5
Бердянск Свежие Объявления — Проблемы ЖКХ
Бердянск Свежие Объявления — Новости Бердянска
```

Also verify there are no collection entries for topics such as advertising, transport, real estate, or services.
