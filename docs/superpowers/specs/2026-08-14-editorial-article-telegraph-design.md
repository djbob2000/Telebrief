# Спецификация: Вечерняя редакционная статья с публикацией в Telegra.ph (Instant View)

## 1. Проблема и цель

В дополнение к структурированному утреннему дайджесту (короткие фактологические тезисы по рубрикам) необходим формат глубокой вечерней журналистской картины дня:
1. **Редакционный лонгрид:** связная аналитическая и информационная статья в стиле городской редакции ([.agents/skills/news-style/SKILL.md](file:///Users/air/develop/Telebrief/.agents/skills/news-style/SKILL.md)), объединяющая все ключевые темы дня (безопасность, ЖКХ, инфраструктура, транспорт, социальные вопросы) с подзаголовками, хронологией, точными адресами, цитатами и атрибуцией источников.
2. **Публикация в Telegra.ph:** автоматическое размещение статьи на платформе Telegra.ph с чистым форматированием (заголовки, цитаты, списки).
3. **Доставка в Telegram (Instant View):** отправка ссылки на публикацию в целевой канал/чат с включенным превью, благодаря чему Telegram автоматически открывает статью в режиме мгновенного просмотра (Instant View).
4. **Независимый запуск:** отдельное вечернее расписание (по умолчанию 20:00) плюс возможность ручного запуска через CLI.

---

## 2. Архитектура решения

```
[Telegram Каналы / Storage] 
       │ (Сбор сырых сообщений за N часов)
       ▼
[Массив сообщений всех каналов] 
       │ (Форматирование в XML-контейнер)
       ▼
[ArticleGenerator] + [System Prompt: article_news_style.txt]
       │ (Генерация Markdown лонгрида через AIProvider)
       ▼
[TelegraphPublisher] 
       │ (Конвертация Markdown -> Telegraph DOM + API createPage)
       ▼
[Telegraph Page URL: https://telegra.ph/...]
       │ 
       ▼
[DigestSender.send_article_instant_view]
       │ (Отправка анонса с включенным Link Preview в Telegram)
       ▼
[Telegram Канал: Пост с кнопкой "Посмотреть" / Instant View]
```

---

## 3. Компоненты и детали реализации

### 3.1. Системный промпт (`src/prompts/article_news_style.txt`)
Промпт реализует все требования [.agents/skills/news-style/SKILL.md](file:///Users/air/develop/Telebrief/.agents/skills/news-style/SKILL.md):
- **Тон и голос:** местная городская редакция («про.бердянск»), практическая польза для жителей, факты, цифры, улицы, микрорайоны, службы (КП, ОСМД).
- **Язык:** русский язык, сохранение украинских официальных названий/цитат при их наличии.
- **Формулы связок:** «Напомним...», «Как мы сообщали ранее...», «По информации очевидцев...», «Официального подтверждения пока нет».
- **Структура статьи:**
  - `# [Заголовок статьи]` (конкретный факт + цифра или ключевое событие дня).
  - Лид-абзац (общая панорама дня).
  - Тематические разделы `##` (Обстановка и происшествия, ЖКХ и ремонты, Транспорт и дороги, Социальная сфера и городская хроника).
  - Заключительный абзац со статусом к 20:00 и последствиями для жителей.

### 3.2. Генератор статьи (`src/article_generator.py`)
Класс `ArticleGenerator`:
- `__init__(config: Config, logger: logging.Logger)`: инициализирует AIProvider и загружает промпт `src/prompts/article_news_style.txt`.
- `_format_messages(messages_by_channel: dict[str, list[Message]]) -> str`: агрегирует сообщения всех каналов в единый хронологический или канальный XML-список с сохранением ссылок, времени и авторов.
- `generate_article(messages_by_channel: dict[str, list[Message]]) -> tuple[str, str, str]`:
  - Вызывает модель с высоким контекстным окном (например, `gemini-3.7-flash` / `deepseek-v4-flash`).
  - Парсит результат: возвращает кортеж `(title, lead, markdown_body)`.

### 3.3. Модуль публикации в Telegra.ph (`src/telegraph.py`)
Класс `TelegraphPublisher`:
- **Управление токеном:**
  - Проверяет наличие токена в конфигурации (`config.settings.article.telegraph_access_token`) или кэш-файле `data/telegraph_token.json`.
  - При отсутствии токена вызывает Telegraph API `createAccount(short_name="Telebrief", author_name="Бердянск Новости")` и сохраняет полученный `access_token`.
- **Конвертер `markdown_to_telegraph_nodes(markdown_text: str) -> list[dict]`**:
  - `#` / `##` $\rightarrow$ `{"tag": "h3", "children": [...]}`
  - `###` / `####` $\rightarrow$ `{"tag": "h4", "children": [...]}`
  - `> quote` $\rightarrow$ `{"tag": "blockquote", "children": [...]}`
  - `- item` $\rightarrow$ `{"tag": "ul", "children": [{"tag": "li", "children": [...]}]}`
  - `\n\n` $\rightarrow$ `{"tag": "p", "children": [...]}`
  - Inline форматирование: `**bold**` $\rightarrow$ `strong`, `*italic*` $\rightarrow$ `em`, `[text](url)` $\rightarrow$ `a`.
- **Публикация:**
  - Метод `async create_page(title: str, content_markdown: str, author_name: str | None = None) -> str`:
    - Вызывает метод Telegraph API `createPage`.
    - Возвращает итоговый URL статьи (например, `https://telegra.ph/V-Berdyanske-likvidiruyut-posledstviya-08-14`).

### 3.4. Отправка Instant View в Telegram (`src/sender.py`)
В `DigestSender` добавляется метод:
- `async send_article_instant_view(title: str, lead: str, telegraph_url: str, user_id: int | None = None) -> bool`:
  - Формирует сообщение с заголовком, кратким лидом и ссылкой на Telegraph.
  - Отправляет с `link_preview_options={"is_disabled": False}` / `disable_web_page_preview=False`, чтобы Telegram сгенерировал плашку **Instant View**.

### 3.5. Планировщик (`src/scheduler.py`)
Расширение `DigestScheduler`:
- Добавляется регистрация отдельной джобы `daily_article`:
  ```python
  if self.config.settings.article.enabled:
      hour, minute = self._parse_schedule_time(self.config.settings.article.schedule_time)
      trigger = CronTrigger(hour=hour, minute=minute, timezone=self.config.settings.timezone)
      self.scheduler.add_job(
          func=self._scheduled_article_job,
          trigger=trigger,
          id="daily_article",
          name="Daily Editorial Article",
          replace_existing=True,
      )
  ```
- Метод `_scheduled_article_job()` выполняет полный цикл генерации и отправки статьи.

### 3.6. Конфигурация (`config.yaml` и `src/config_loader.py`)
В `settings`:
```yaml
settings:
  # ... существующие настройки дайджеста ...

  article:
    enabled: true
    schedule_time: "20:00"
    lookback_hours: 24
    author_name: "Бердянск Новости"
    fallback_save_dir: "data/articles"
    # telegraph_access_token: "" # опционально
```

### 3.7. CLI и ручной запуск (`main.py`)
Поддержка аргументов командной строки:
- `python main.py --article` — сбор сообщений за 24ч, генерация статьи, публикация в Telegraph и отправка Instant View.
- `python main.py --article --hours 12` — запуск за произвольный период.
- `python -m src.article_generator --hours 24` — прямой запуск модуля.

---

## 4. Обработка ошибок и резервирование (Fault Tolerance)

1. **Нет сообщений:** Если за период `lookback_hours` не собрано сообщений, генерация пропускается, в лог пишется `warning`.
2. **Сбой Telegraph API:** Если Telegraph временно недоступен или вернул ошибку, статья сохраняется локально в `data/articles/YYYY-MM-DD_HH-MM_editorial.md`, а в канал отправляется текстовое уведомление или резервное сообщение.
3. **Изоляция:** Ошибки статьи никак не затрагивают утренний дайджест и процесс сбора сообщений.

---

## 5. План тестирования

1. **Тесты конвертера Telegraph DOM (`tests/test_telegraph.py`)**:
   - Корректная конвертация заголовков, цитат, списков, ссылок и форматирования в DOM-структуру Telegraph.
   - Тест API-клиента Telegraph с моком `aiohttp.ClientSession` (создание аккаунта, создание страницы).
2. **Тесты генератора статьи (`tests/test_article_generator.py`)**:
   - Форматирование входных сообщений в промпт.
   - Парсинг заголовка, лида и тела статьи из ответа AI.
   - Обработка пустых сообщений и ошибок провайдера.
3. **Тесты планировщика (`tests/test_scheduler.py`)**:
   - Корректная инициализация двух независимых Cron-заданий (`daily_digest` и `daily_article`).
4. **Тесты отправки (`tests/test_sender.py`)**:
   - Отправка сообщения с включенным превью ссылки для Instant View.
