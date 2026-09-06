# Evening Editorial Article & Telegra.ph Instant View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily evening editorial long-form article generation pipeline that collects raw channel messages, creates a local-news article adhering to `.agents/skills/news-style/SKILL.md`, publishes it to Telegra.ph via the Telegraph API, and broadcasts an Instant View announcement link to the Telegram channel at 20:00 every day (and on-demand via CLI).

**Architecture:** A dedicated `ArticleGenerator` formats raw messages and calls `AIProvider` using a newsroom system prompt (`src/prompts/article_news_style.txt`). A lightweight `TelegraphPublisher` converts generated Markdown into Telegraph DOM nodes and interacts with Telegraph REST API (`createAccount` / `createPage`). `DigestSender` is augmented with `send_article_instant_view` to deliver the link preview, and `DigestScheduler` registers a distinct cron job (`daily_article`) alongside the morning digest.

**Tech Stack:** Python 3.14, aiohttp (async Telegraph API), apscheduler (cron job), python-telegram-bot (delivery), pytest / pytest-asyncio (tests).

---

### File Structure Map

- **New Files**:
  - `src/prompts/article_news_style.txt` — System prompt capturing the full `news-style/SKILL.md` editorial contract.
  - `src/telegraph.py` — `TelegraphPublisher` class and `markdown_to_telegraph_nodes` converter.
  - `src/article_generator.py` — `ArticleGenerator` class for assembling message XML, querying LLM, and parsing article structure.
  - `tests/test_telegraph.py` — Unit tests for markdown conversion and Telegraph API integration.
  - `tests/test_article_generator.py` — Unit tests for message XML formatting, parsing, and error cases.
- **Modified Files**:
  - `src/config_loader.py` — Add `ArticleConfig` model and attach to `SettingsConfig`.
  - `config.yaml` & `config.yaml.example` — Add `article:` section with schedule and options.
  - `src/sender.py` — Add `send_article_instant_view(title, lead, telegraph_url, user_id=None)` method.
  - `src/core.py` — Add `generate_and_publish_article(config, logger, hours=24)` pipeline function.
  - `src/scheduler.py` — Add `daily_article` cron job at `config.settings.article.schedule_time`.
  - `main.py` — Add CLI argument parsing for `--article` and `--hours`.
  - `tests/test_sender.py` — Tests for `send_article_instant_view`.
  - `tests/test_core.py` — Tests for `generate_and_publish_article`.
  - `tests/test_scheduler.py` — Tests for article scheduler job registration.

---

### Task 1: Configuration Model & Default Settings for Article

**Files:**
- Modify: `src/config_loader.py`
- Modify: `config.yaml`
- Modify: `config.yaml.example`
- Test: `tests/test_config_loader.py`

- [ ] **Step 1: Write failing test for ArticleConfig in test_config_loader.py**

```python
def test_article_config_defaults():
    from src.config_loader import ArticleConfig, SettingsConfig
    cfg = SettingsConfig()
    assert cfg.article.enabled is True
    assert cfg.article.schedule_time == "20:00"
    assert cfg.article.lookback_hours == 24
    assert cfg.article.author_name == "Бердянск Новости"
    assert cfg.article.fallback_save_dir == "data/articles"
    assert cfg.article.telegraph_access_token is None


def test_article_config_custom_values():
    from src.config_loader import ConfigLoader
    yaml_content = """
    channels:
      - id: "@test"
        name: "Test"
    settings:
      article:
        enabled: false
        schedule_time: "21:30"
        lookback_hours: 12
        author_name: "Custom Editor"
        fallback_save_dir: "custom/dir"
        telegraph_access_token: "tok123"
    """
    loader = ConfigLoader()
    config = loader.load_from_string(yaml_content)
    assert config.settings.article.enabled is False
    assert config.settings.article.schedule_time == "21:30"
    assert config.settings.article.lookback_hours == 12
    assert config.settings.article.author_name == "Custom Editor"
    assert config.settings.article.fallback_save_dir == "custom/dir"
    assert config.settings.article.telegraph_access_token == "tok123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_loader.py -k "test_article_config" -v`
Expected: FAIL with `AttributeError` or `ImportError: cannot import name 'ArticleConfig'`

- [ ] **Step 3: Implement ArticleConfig dataclass and add to SettingsConfig**

In `src/config_loader.py`:
```python
@dataclass
class ArticleConfig:
    """Configuration for daily editorial article generation."""
    enabled: bool = True
    schedule_time: str = "20:00"
    lookback_hours: int = 24
    author_name: str = "Бердянск Новости"
    fallback_save_dir: str = "data/articles"
    telegraph_access_token: Optional[str] = None
```
Add `article: ArticleConfig = field(default_factory=ArticleConfig)` to `SettingsConfig`.
Update parser in `ConfigLoader._parse_settings` to populate `ArticleConfig`.

In `config.yaml` and `config.yaml.example`:
```yaml
  article:
    enabled: true
    schedule_time: "20:00"
    lookback_hours: 24
    author_name: "Бердянск Новости"
    fallback_save_dir: "data/articles"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config_loader.py config.yaml config.yaml.example tests/test_config_loader.py
git commit -m "feat: add article configuration schema and defaults"
```

---

### Task 2: News Style Editorial System Prompt

**Files:**
- Create: `src/prompts/article_news_style.txt`
- Test: `tests/test_article_generator.py`

- [ ] **Step 1: Create system prompt template**

Create `src/prompts/article_news_style.txt` encoding the full editorial contract from `.agents/skills/news-style/SKILL.md`:
- Focus: Berdyansk & Berdyansk district.
- Tone: Local newsroom style (pro.berdyansk.biz), practical, fact-led, neutral attribution.
- Language: {language} (Russian), preserving original Ukrainian names/quotes when present.
- Structure:
  1. `# [Заголовок статьи]` (конкретный факт + цифра или ключевое событие дня).
  2. Вводный лид (2-3 предложения).
  3. Тематические блоки с подзаголовками `##`:
     - Обстановка в городе и безопасность
     - ЖКХ, электроснабжение, водоснабжение и ремонты сетей
     - Транспорт, дороги, связь и социальная сфера
     - Городская хроника и обращения жителей
  4. Заключительный абзац (итоги дня и последствия для жителей к вечеру).
- Style markers: «Как мы сообщали ранее...», «Напомним,...», «Отметим,...», «По информации...».
- Rules: No chat preambles, no meta text, no invented facts, distinguish claims from verified facts.

- [ ] **Step 2: Write test verifying prompt existence and key contract markers**

In `tests/test_article_generator.py`:
```python
from pathlib import Path

def test_article_prompt_template_exists_and_contains_rules():
    prompt_path = Path("src/prompts/article_news_style.txt")
    assert prompt_path.exists()
    content = prompt_path.read_text(encoding="utf-8")
    assert "{language}" in content
    assert "Бердянск" in content
    assert "Напомним" in content
    assert "##" in content
```

- [ ] **Step 3: Run test to verify it passes**

Run: `uv run pytest tests/test_article_generator.py -k "test_article_prompt" -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/prompts/article_news_style.txt tests/test_article_generator.py
git commit -m "feat: add article news style system prompt"
```

---

### Task 3: Telegraph Markdown-to-DOM Converter & Telegraph Publisher

**Files:**
- Create: `src/telegraph.py`
- Test: `tests/test_telegraph.py`

- [ ] **Step 1: Write failing tests for Markdown-to-DOM conversion and Telegraph API**

In `tests/test_telegraph.py`:
```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.telegraph import TelegraphPublisher, markdown_to_telegraph_nodes

def test_markdown_to_telegraph_nodes_headers_and_paragraphs():
    md = """# Main Title
Intro paragraph with **bold** and *italic* and [link](https://t.me/test).

## Subhead Section
Second paragraph with quote:
> Quote text here

- Bullet 1
- Bullet 2
"""
    nodes = markdown_to_telegraph_nodes(md)
    assert len(nodes) > 0
    # Check for h3 / h4 tags
    tags = [n.get("tag") for n in nodes if isinstance(n, dict)]
    assert "h3" in tags or "h4" in tags
    assert "p" in tags
    assert "blockquote" in tags
    assert "ul" in tags


@pytest.mark.asyncio
async def test_telegraph_publisher_create_page_success():
    publisher = TelegraphPublisher(access_token="dummy_token")
    
    mock_response = {
        "ok": True,
        "result": {
            "path": "Sample-Article-08-14",
            "url": "https://telegra.ph/Sample-Article-08-14",
            "title": "Sample Article"
        }
    }
    
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.json = AsyncMock(return_value=mock_response)
        mock_post.return_value.__aenter__.return_value = mock_resp_obj
        
        url = await publisher.create_page(
            title="Sample Article",
            content_markdown="# Sample Article\n\nContent paragraph.",
            author_name="Бердянск Новости"
        )
        assert url == "https://telegra.ph/Sample-Article-08-14"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegraph.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.telegraph'`

- [ ] **Step 3: Implement `markdown_to_telegraph_nodes` and `TelegraphPublisher`**

In `src/telegraph.py`:
```python
"""
Telegra.ph publisher and Markdown to Telegraph DOM converter.
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiohttp

TELEGRAPH_API_BASE = "https://api.telegra.ph"
DEFAULT_TOKEN_PATH = Path("data/telegraph_token.json")


def _parse_inline(text: str) -> List[Any]:
    """Parse inline bold, italic, and links into Telegraph node format."""
    # Pattern matching bold (**text**), italic (*text*), link ([text](url))
    pattern = re.compile(r'(\*\*.*?\*\*|\*.*?\*|\[.*?\]\(.*?\))')
    parts = pattern.split(text)
    result: List[Any] = []
    
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) >= 4:
            result.append({"tag": "strong", "children": _parse_inline(part[2:-2])})
        elif part.startswith("*") and part.endswith("*") and len(part) >= 2:
            result.append({"tag": "em", "children": _parse_inline(part[1:-1])})
        elif part.startswith("[") and "](" in part and part.endswith(")"):
            bracket_end = part.find("](")
            link_text = part[1:bracket_end]
            url = part[bracket_end + 2 : -1]
            result.append({"tag": "a", "attrs": {"href": url}, "children": [link_text]})
        else:
            result.append(part)
    return result


def markdown_to_telegraph_nodes(markdown_text: str) -> List[Dict[str, Any]]:
    """Convert Markdown text to Telegraph DOM nodes."""
    nodes: List[Dict[str, Any]] = []
    lines = markdown_text.strip().splitlines()
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        
        # Headers
        if line.startswith("# ") or line.startswith("## "):
            header_text = re.sub(r"^#+\s*", "", line)
            nodes.append({"tag": "h3", "children": _parse_inline(header_text)})
            i += 1
            continue
        elif line.startswith("### ") or line.startswith("#### "):
            header_text = re.sub(r"^#+\s*", "", line)
            nodes.append({"tag": "h4", "children": _parse_inline(header_text)})
            i += 1
            continue
        
        # Blockquotes
        if line.startswith(">"):
            quote_lines = []
            while i < len(lines) and (lines[i].strip().startswith(">") or (lines[i].strip() and quote_lines)):
                ql = re.sub(r"^>\s*", "", lines[i].strip())
                quote_lines.append(ql)
                i += 1
                if i < len(lines) and not lines[i].strip():
                    break
            quote_text = " ".join(quote_lines)
            nodes.append({"tag": "blockquote", "children": _parse_inline(quote_text)})
            continue
        
        # Unordered Lists
        if line.startswith("- ") or line.startswith("* ") or line.startswith("• "):
            list_items = []
            while i < len(lines) and (lines[i].strip().startswith(("- ", "* ", "• "))):
                item_text = re.sub(r"^[-*•]\s*", "", lines[i].strip())
                list_items.append({"tag": "li", "children": _parse_inline(item_text)})
                i += 1
            nodes.append({"tag": "ul", "children": list_items})
            continue
        
        # Paragraphs
        para_lines = []
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(("#", ">", "- ", "* ", "• ")):
            para_lines.append(lines[i].strip())
            i += 1
        if para_lines:
            para_text = " ".join(para_lines)
            nodes.append({"tag": "p", "children": _parse_inline(para_text)})
            
    return nodes


class TelegraphPublisher:
    """Async client for publishing articles to Telegra.ph."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        token_file: Path = DEFAULT_TOKEN_PATH,
    ):
        self.access_token = access_token
        self.logger = logger or logging.getLogger(__name__)
        self.token_file = token_file

    async def get_or_create_access_token(
        self, short_name: str = "Telebrief", author_name: str = "Бердянск Новости"
    ) -> str:
        """Retrieve cached access token or create a new Telegraph account."""
        if self.access_token:
            return self.access_token
        
        if self.token_file.exists():
            try:
                data = json.loads(self.token_file.read_text(encoding="utf-8"))
                token = data.get("access_token")
                if token:
                    self.access_token = token
                    return token
            except Exception as e:
                self.logger.warning(f"Failed to read cached Telegraph token: {e}")

        # Create account via API
        async with aiohttp.ClientSession() as session:
            payload = {
                "short_name": short_name,
                "author_name": author_name,
            }
            async with session.post(f"{TELEGRAPH_API_BASE}/createAccount", json=payload) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegraph createAccount failed: {data.get('error')}")
                token = data["result"]["access_token"]
                self.access_token = token
                try:
                    self.token_file.parent.mkdir(parents=True, exist_ok=True)
                    self.token_file.write_text(json.dumps({"access_token": token}), encoding="utf-8")
                except Exception as e:
                    self.logger.warning(f"Could not persist Telegraph token: {e}")
                return token

    async def create_page(
        self,
        title: str,
        content_markdown: str,
        author_name: str = "Бердянск Новости",
        author_url: Optional[str] = None,
    ) -> str:
        """Publish article to Telegra.ph and return the page URL."""
        token = await self.get_or_create_access_token(author_name=author_name)
        nodes = markdown_to_telegraph_nodes(content_markdown)
        
        payload = {
            "access_token": token,
            "title": title[:256],
            "author_name": author_name,
            "content": nodes,
            "return_content": False,
        }
        if author_url:
            payload["author_url"] = author_url

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{TELEGRAPH_API_BASE}/createPage", json=payload) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegraph createPage failed: {data.get('error')}")
                return data["result"]["url"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telegraph.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/telegraph.py tests/test_telegraph.py
git commit -m "feat: implement Telegraph publisher and Markdown to DOM converter"
```

---

### Task 4: ArticleGenerator Implementation

**Files:**
- Create: `src/article_generator.py`
- Test: `tests/test_article_generator.py`

- [ ] **Step 1: Write failing test for `ArticleGenerator`**

In `tests/test_article_generator.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone
from src.collector import Message
from src.config_loader import Config, SettingsConfig, ArticleConfig
from src.article_generator import ArticleGenerator

@pytest.mark.asyncio
async def test_article_generator_creates_valid_article():
    config = Config()
    config.settings.output_language = "Russian"
    logger = MagicMock()
    
    mock_ai_response = """# В Бердянске ликвидируют последствия перебоев со светом и водой

За прошедшие сутки в Бердянске ключевые коммунальные службы работали в усиленном режиме из-за ночных инцидентов.

## Обстановка в городе и происшествия
По сообщениям жителей, около полуночи в районе Косы были слышны громкие звуки.

## Коммунальная инфраструктура
Как сообщили в аварийной службе, ремонтные бригады восстановили подачу электричества в 12 домах на улице Победы.
"""
    
    generator = ArticleGenerator(config, logger)
    generator.provider.chat_completion = AsyncMock(return_value=mock_ai_response)
    
    messages_by_channel = {
        "Бердянск": [
            Message(
                id=1,
                channel="Бердянск",
                sender="Admin",
                text="В районе Косы слышны громкие звуки",
                timestamp=datetime.now(timezone.utc),
                link="https://t.me/berdiansk_me/100"
            )
        ]
    }
    
    title, lead, body = await generator.generate_article(messages_by_channel)
    assert "В Бердянске ликвидируют последствия" in title
    assert "коммунальные службы работали" in lead
    assert "## Обстановка в городе" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_article_generator.py -k "test_article_generator" -v`
Expected: FAIL with `ImportError: cannot import name 'ArticleGenerator'`

- [ ] **Step 3: Implement `ArticleGenerator` class**

In `src/article_generator.py`:
```python
"""
Editorial article generator adhering to news-style local journalism rules.
"""
import html
import logging
from pathlib import Path
from typing import Dict, List, Tuple

from src.ai_providers import AIProvider, create_provider
from src.collector import Message
from src.config_loader import Config
from src.xml_escape import escape_xml_delimiters

_DEFAULT_ARTICLE_PROMPT_PATH = str(Path(__file__).parent / "prompts" / "article_news_style.txt")


class ArticleGenerator:
    """Generates extended editorial news articles from raw Telegram messages."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.provider: AIProvider = create_provider(
            provider_name=config.settings.ai_provider,
            logger=logger,
            openai_api_key=config.openai_api_key,
            openai_base_url=config.openai_base_url,
            anthropic_api_key=config.anthropic_api_key,
            google_api_key=config.google_api_key,
            ollama_base_url=config.settings.ollama_base_url,
            api_timeout=config.settings.api_timeout,
        )
        self.model = config.settings.ai_model
        self.temperature = 0.4
        self.max_tokens = 32000
        self.output_language = config.settings.output_language
        
        prompt_path = Path(_DEFAULT_ARTICLE_PROMPT_PATH)
        if not prompt_path.exists():
            raise FileNotFoundError(f"Article prompt template not found: {prompt_path}")
        self.base_prompt_template = prompt_path.read_text(encoding="utf-8")

    def _format_messages_for_prompt(
        self, messages_by_channel: Dict[str, List[Message]], max_chars: int = 150000
    ) -> str:
        """Format all channel messages into an XML structure."""
        formatted_channels = []
        for channel_name, messages in messages_by_channel.items():
            if not messages:
                continue
            lines = []
            for i, msg in enumerate(messages, 1):
                timestamp = msg.timestamp.strftime("%H:%M")
                text = (
                    msg.text.replace("\r", " ")
                    .replace("\n", " ")
                    .replace(" | ", " - ")
                )
                sender = msg.sender.replace("\r", " ").replace("\n", " ").replace(" | ", " - ")
                link = msg.link if msg.link and msg.link != "#" else ""
                link_part = f" | {link}" if link else ""
                lines.append(f"{i}. [{timestamp}] {sender}: {text}{link_part}")
            
            safe_name = html.escape(channel_name, quote=True)
            escaped_content = escape_xml_delimiters("\n".join(lines))
            formatted_channels.append(
                f'<channel_messages channel="{safe_name}" total="{len(lines)}">\n{escaped_content}\n</channel_messages>'
            )
        
        joined = "\n\n".join(formatted_channels)
        return joined[:max_chars]

    def _parse_article_response(self, text: str) -> Tuple[str, str, str]:
        """Extract title, lead, and clean markdown body from AI output."""
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        title = "Редакционная картина дня в Бердянске"
        lead = ""
        body_lines = []
        
        found_title = False
        for line in lines:
            if line.startswith("# ") and not found_title:
                title = line[2:].strip()
                found_title = True
            elif found_title and not lead and not line.startswith("#"):
                lead = line
                body_lines.append(line)
            else:
                body_lines.append(line)
                
        body = text.strip()
        return title, lead, body

    async def generate_article(
        self, messages_by_channel: Dict[str, List[Message]]
    ) -> Tuple[str, str, str]:
        """Generate long-form article from channel messages."""
        total_msgs = sum(len(msgs) for msgs in messages_by_channel.values())
        if total_msgs == 0:
            raise ValueError("No messages provided for article generation")

        system_prompt = self.base_prompt_template.format(language=self.output_language)
        formatted_messages = self._format_messages_for_prompt(messages_by_channel)
        
        user_prompt = f"""Сформируй полную редакционную статью-картину дня по мотивам сообщений ниже.

<all_channel_data total_messages="{total_msgs}">
{formatted_messages}
</all_channel_data>
"""
        chat_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        
        self.logger.info(f"Generating editorial article from {total_msgs} messages")
        response = await self.provider.chat_completion(
            messages=chat_messages,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort="high",
            thinking=True,
        )
        return self._parse_article_response(response)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_article_generator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/article_generator.py tests/test_article_generator.py
git commit -m "feat: implement ArticleGenerator pipeline and message formatting"
```

---

### Task 5: Instant View Delivery in DigestSender

**Files:**
- Modify: `src/sender.py`
- Modify: `tests/test_sender.py`

- [ ] **Step 1: Write failing test in `tests/test_sender.py`**

```python
@pytest.mark.asyncio
async def test_send_article_instant_view():
    from src.config_loader import Config
    from src.sender import DigestSender
    
    config = Config()
    config.telegram_bot_token = "dummy_token"
    config.settings.target_user_id = 12345
    logger = MagicMock()
    
    sender = DigestSender(config, logger)
    sender.bot = AsyncMock()
    
    success = await sender.send_article_instant_view(
        title="В Бердянске ликвидируют последствия происшествий",
        lead="Краткий лид статьи о ситуации в городе за последние 24 часа.",
        telegraph_url="https://telegra.ph/V-Berdyanske-08-14",
        user_id=12345
    )
    assert success is True
    sender.bot.send_message.assert_called_once()
    kwargs = sender.bot.send_message.call_args.kwargs
    assert kwargs["disable_web_page_preview"] is False
    assert "https://telegra.ph/V-Berdyanske-08-14" in kwargs["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sender.py -k "test_send_article_instant_view" -v`
Expected: FAIL with `AttributeError: 'DigestSender' object has no attribute 'send_article_instant_view'`

- [ ] **Step 3: Implement `send_article_instant_view` in `src/sender.py`**

In `src/sender.py`:
```python
    async def send_article_instant_view(
        self,
        title: str,
        lead: str,
        telegraph_url: str,
        user_id: Optional[int] = None,
    ) -> bool:
        """
        Send an editorial article announcement with Telegram Instant View preview.

        Args:
            title: Headline of the article
            lead: Short introductory lead text
            telegraph_url: Telegra.ph page URL
            user_id: Optional target user or chat ID

        Returns:
            True if message was sent successfully
        """
        chat_id = user_id or self.target_chat_id or self.target_user_id
        if not chat_id:
            self.logger.error("No target chat_id or user_id configured for article delivery")
            return False

        message_text = (
            f"📰 *{title}*\n\n"
            f"{lead}\n\n"
            f"⚡️ [Читать полностью в Instant View]({telegraph_url})\n"
            f"{telegraph_url}"
        )
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )
            self.logger.info(f"Sent article Instant View to {chat_id}")
            return True
        except TelegramError as e:
            self.logger.error(f"Failed to send article Instant View: {e}")
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sender.py -k "test_send_article_instant_view" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sender.py tests/test_sender.py
git commit -m "feat: add send_article_instant_view method to DigestSender"
```

---

### Task 6: Core Orchestration (`generate_and_publish_article`)

**Files:**
- Modify: `src/core.py`
- Modify: `tests/test_core.py`

- [ ] **Step 1: Write failing test in `tests/test_core.py`**

```python
@pytest.mark.asyncio
async def test_generate_and_publish_article_workflow():
    from src.config_loader import Config
    from src.core import generate_and_publish_article
    
    config = Config()
    config.telegram_bot_token = "dummy"
    config.settings.target_user_id = 123
    logger = MagicMock()
    
    with patch("src.core._collect_messages", new_callable=AsyncMock) as mock_collect, \
         patch("src.article_generator.ArticleGenerator.generate_article", new_callable=AsyncMock) as mock_gen, \
         patch("src.telegraph.TelegraphPublisher.create_page", new_callable=AsyncMock) as mock_page, \
         patch("src.sender.DigestSender.send_article_instant_view", new_callable=AsyncMock) as mock_send:
        
        mock_collect.return_value = {"ch1": [MagicMock()]}
        mock_gen.return_value = ("Заголовок", "Лид", "# Заголовок\n\nТело статьи.")
        mock_page.return_value = "https://telegra.ph/art-123"
        mock_send.return_value = True
        
        res = await generate_and_publish_article(config, logger, hours=24)
        assert res is True
        mock_collect.assert_called_once()
        mock_gen.assert_called_once()
        mock_page.assert_called_once()
        mock_send.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_core.py -k "test_generate_and_publish_article" -v`
Expected: FAIL with `ImportError: cannot import name 'generate_and_publish_article'`

- [ ] **Step 3: Implement `generate_and_publish_article` in `src/core.py`**

In `src/core.py`:
```python
async def generate_and_publish_article(
    config: Config,
    logger: logging.Logger,
    hours: int = 24,
    user_id: Optional[int] = None,
) -> bool:
    """
    Collect messages, generate long-form editorial article, publish to Telegra.ph,
    and broadcast Instant View announcement to Telegram.
    """
    validate_hours(hours)
    start_time = datetime.now(timezone.utc)
    logger.info(f"Starting evening editorial article workflow for last {hours} hours")
    
    messages_by_channel = await _collect_messages(config, logger, hours)
    total_messages = sum(len(msgs) for msgs in messages_by_channel.values())
    if total_messages == 0:
        logger.warning("No messages collected for article generation")
        return False
    
    from src.article_generator import ArticleGenerator
    from src.telegraph import TelegraphPublisher
    
    generator = ArticleGenerator(config, logger)
    title, lead, markdown_body = await generator.generate_article(messages_by_channel)
    
    # Fallback local save in case Telegraph is unreachable
    fallback_dir = Path(config.settings.article.fallback_save_dir)
    try:
        fallback_dir.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
        fallback_file = fallback_dir / f"{now_str}_editorial.md"
        fallback_file.write_text(markdown_body, encoding="utf-8")
        logger.info(f"Saved local fallback copy of article to {fallback_file}")
    except Exception as e:
        logger.warning(f"Could not save local article backup: {e}")

    publisher = TelegraphPublisher(
        access_token=config.settings.article.telegraph_access_token,
        logger=logger,
    )
    telegraph_url = await publisher.create_page(
        title=title,
        content_markdown=markdown_body,
        author_name=config.settings.article.author_name,
    )
    logger.info(f"Published article to Telegra.ph: {telegraph_url}")
    
    sender = DigestSender(config, logger)
    success = await sender.send_article_instant_view(
        title=title,
        lead=lead,
        telegraph_url=telegraph_url,
        user_id=user_id,
    )
    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(f"Article workflow finished in {duration:.1f}s (success={success})")
    return success
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_core.py -k "test_generate_and_publish_article" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/core.py tests/test_core.py
git commit -m "feat: add generate_and_publish_article orchestration workflow"
```

---

### Task 7: Scheduler & CLI Integration

**Files:**
- Modify: `src/scheduler.py`
- Modify: `main.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write test for article job in `tests/test_scheduler.py`**

In `tests/test_scheduler.py`:
```python
def test_scheduler_registers_both_digest_and_article_jobs():
    from src.config_loader import Config
    from src.scheduler import DigestScheduler
    
    config = Config()
    config.settings.schedule_time = "09:00"
    config.settings.article.enabled = True
    config.settings.article.schedule_time = "20:00"
    logger = MagicMock()
    
    scheduler = DigestScheduler(config, logger)
    scheduler.start()
    try:
        digest_job = scheduler.scheduler.get_job("daily_digest")
        article_job = scheduler.scheduler.get_job("daily_article")
        assert digest_job is not None
        assert article_job is not None
    finally:
        scheduler.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler.py -k "test_scheduler_registers" -v`
Expected: FAIL with `AssertionError: assert None is not None`

- [ ] **Step 3: Update `src/scheduler.py` and `main.py`**

In `src/scheduler.py`:
Add job registration in `start()`:
```python
        if self.config.settings.article.enabled:
            art_hour, art_min = self._parse_schedule_time(self.config.settings.article.schedule_time)
            art_trigger = CronTrigger(hour=art_hour, minute=art_min, timezone=self.config.settings.timezone)
            self.scheduler.add_job(
                func=self._scheduled_article_job,
                trigger=art_trigger,
                id="daily_article",
                name="Daily Editorial Article",
                replace_existing=True,
            )
```
Add `_scheduled_article_job`:
```python
    async def _scheduled_article_job(self):
        """Run daily editorial article generation and delivery."""
        self.logger.info("=" * 60)
        self.logger.info("📰 SCHEDULED ARTICLE JOB STARTED")
        self.logger.info("=" * 60)
        try:
            from src.core import generate_and_publish_article
            success = await generate_and_publish_article(
                config=self.config,
                logger=self.logger,
                hours=self.config.settings.article.lookback_hours,
            )
            if success:
                self.logger.info("✅ Scheduled article completed successfully")
            else:
                self.logger.error("❌ Scheduled article failed")
        except Exception as e:
            self.logger.error(f"❌ Scheduled article job failed: {e}", exc_info=True)
```

In `main.py`:
Add `--article` argument to argument parser and execute `generate_and_publish_article`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scheduler.py main.py tests/test_scheduler.py
git commit -m "feat: register daily_article scheduler job and add CLI --article flag"
```

---

### Task 8: Full Verification & Integration Test

**Files:**
- Run all test suites
- Test formatting and linting

- [ ] **Step 1: Run complete test suite**

Run: `uv run pytest`
Expected: All tests PASS with coverage >= 80%

- [ ] **Step 2: Run linters (flake8, black, isort)**

Run: `uv run black --check src tests && uv run isort --check src tests`
Expected: Clean formatting without errors.
