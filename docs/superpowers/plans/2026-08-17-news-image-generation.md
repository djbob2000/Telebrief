# News Editorial Image Generation & Telegram Photo Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement automatic AI editorial image generation and Telegram photo post delivery with inline button for long-form news articles.

**Architecture:** A new `NewsImageGenerator` class creates prompt via LLM (anchored by `news-image-redraw` skill) and queries Google Gemini Image API (`gemini-3.1-flash-lite-image`). `DigestSender` is enhanced with `send_article_with_photo` using Telegram Bot API `sendPhoto` and `InlineKeyboardMarkup`, with seamless fallback to text if image generation/sending fails.

**Tech Stack:** Python 3.10+, `python-telegram-bot`, `httpx`, `pytest`, `pytest-asyncio`.

---

### Task 1: Create `src/image_generator.py` and Unit Tests

**Files:**
- Create: `src/image_generator.py`
- Create: `tests/test_image_generator.py`

- [ ] **Step 1: Write failing unit tests for `NewsImageGenerator`**

```python
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from src.image_generator import NewsImageGenerator


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.gemini_api_key = "test-gemini-key"
    config.settings.ai_provider = "google"
    config.settings.ai_model = "gemini-3.7-flash"
    config.settings.article.fallback_save_dir = "data/articles"
    return config


@pytest.fixture
def mock_logger():
    return MagicMock()


@pytest.mark.asyncio
async def test_generate_prompt_formats_correctly(mock_config, mock_logger):
    generator = NewsImageGenerator(mock_config, mock_logger)
    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = (
        "Realistic editorial photojournalism, documentary street photography. "
        "A residential courtyard in Berdyansk, Ukraine during a blackout. "
        "STRICTLY NO text, NO letters. 16:9 aspect ratio."
    )
    generator.ai_provider = mock_provider

    prompt = await generator.generate_prompt(
        title="Блэкаут в Бердянске",
        lead="В городе отключили свет и воду.",
        article_text="Подробный текст статьи...",
        city_name="Бердянск",
    )

    assert "Berdyansk" in prompt or "blackout" in prompt
    assert mock_provider.chat_completion.called


@pytest.mark.asyncio
async def test_generate_image_saves_valid_file(mock_config, mock_logger, tmp_path):
    generator = NewsImageGenerator(mock_config, mock_logger)
    fake_png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    fake_b64 = base64.b64encode(fake_png_bytes).decode("utf-8")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/jpeg",
                                "data": fake_b64,
                            }
                        }
                    ]
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        saved_path = await generator.generate_image(
            prompt="A residential courtyard in Berdyansk",
            output_dir=tmp_path,
        )

    assert saved_path is not None
    assert saved_path.exists()
    assert saved_path.read_bytes() == fake_png_bytes


@pytest.mark.asyncio
async def test_generate_image_handles_quota_gracefully(mock_config, mock_logger, tmp_path):
    generator = NewsImageGenerator(mock_config, mock_logger)
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = "Quota exceeded"

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        saved_path = await generator.generate_image(
            prompt="A residential courtyard in Berdyansk",
            output_dir=tmp_path,
        )

    assert saved_path is None
    mock_logger.warning.assert_called()
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_image_generator.py -v`
Expected: FAIL (ModuleNotFoundError: No module named 'src.image_generator')

- [ ] **Step 3: Implement `src/image_generator.py`**

```python
"""AI Image generation module for news articles."""

import base64
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Optional

import httpx

from src.ai_providers import GoogleProvider
from src.config_loader import Config


IMAGE_PROMPT_SYSTEM_INSTRUCTION = """You are an expert editorial art director for a local newsroom.
Your task is to create a detailed, photorealistic image generation prompt in English based on the provided news article headline, lead, and details.

Follow these strict rules:
1. PRIMARY STYLE: Realistic editorial photojournalism, documentary street/indoor photography, 35mm lens, candid natural shot.
2. REGIONAL AUTHENTICITY: Anchor the scene to the authentic post-Soviet / Eastern European reality of {city_name}, Ukraine (e.g. Soviet-era 5-story brick/panel apartment blocks (khrushchevka), authentic asphalt courtyards with poplars/acacias, typical entrance canopies, gas pipes on facades).
3. DOMESTIC / INDOOR ADVANTAGE: If appropriate, prefer domestic interior scenes (kitchens with flashlights/powerbanks, water canisters, balconies) or close-up action shots.
4. STRICT NEGATIVE CONSTRAINTS: NO text, NO letters, NO words, NO signs on shops or t-shirts, NO American architecture, NO Western suburban streets, NO palm trees, NO watermarks, NO artificial glossy stock-photo look.
5. ASPECT RATIO: Always include "16:9 aspect ratio".

Output ONLY the final English prompt text without explanations or markdown formatting."""


class NewsImageGenerator:
    """Generates editorial illustrations for news articles."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.api_key = getattr(config, "gemini_api_key", None) or getattr(
            config, "google_api_key", None
        )
        self.ai_provider = (
            GoogleProvider(api_key=self.api_key, logger=logger) if self.api_key else None
        )

    async def generate_prompt(
        self,
        title: str,
        lead: str,
        article_text: str,
        city_name: str = "Бердянск",
    ) -> str:
        """Generate an English visual prompt for image generation models."""
        system_instruction = IMAGE_PROMPT_SYSTEM_INSTRUCTION.format(city_name=city_name)
        user_content = (
            f"Заголовок новости: {title}\n"
            f"Лид новости: {lead}\n"
            f"Краткий контекст статьи: {article_text[:1500]}\n\n"
            f"Сформируй один связный детальный промпт на английском языке для генерации изображения 16:9."
        )

        if self.ai_provider is None:
            self.logger.warning("No AI provider available for prompt generation, using fallback.")
            return (
                f"Realistic editorial photojournalism, documentary photography. "
                f"A residential courtyard in {city_name}, Ukraine with Soviet-era brick apartment buildings. "
                f"STRICTLY NO text, NO letters, NO words, NO logos. 16:9 aspect ratio."
            )

        try:
            model = getattr(self.config.settings, "ai_model", "gemini-3.7-flash")
            response = await self.ai_provider.chat_completion(
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_content},
                ],
                model=model,
                temperature=0.7,
                max_tokens=500,
            )
            clean_prompt = response.strip().strip('"').strip("'")
            self.logger.info("Generated visual prompt for article")
            return clean_prompt
        except Exception as e:
            self.logger.warning(f"Failed to generate prompt via LLM: {e}, using fallback.")
            return (
                f"Realistic editorial photojournalism, documentary street photography. "
                f"A residential courtyard in {city_name}, Ukraine with Soviet-era brick apartment buildings. "
                f"STRICTLY NO text, NO letters, NO words, NO logos. 16:9 aspect ratio."
            )

    async def generate_image(
        self,
        prompt: str,
        output_dir: Optional[Path | str] = None,
        model_name: str = "gemini-3.1-flash-lite-image",
    ) -> Optional[Path]:
        """Generate image via Gemini Image API and save to disk."""
        if not self.api_key:
            self.logger.warning("No Gemini API key available for image generation")
            return None

        if output_dir is None:
            base_dir = getattr(
                self.config.settings.article, "fallback_save_dir", "data/articles"
            )
            output_dir = Path(base_dir) / "images"
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_file = output_dir / f"editorial_{timestamp}.jpg"

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    self.logger.warning(
                        f"Image generation API returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                    return None

                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    self.logger.warning("Image generation returned no candidates")
                    return None

                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    inline_data = part.get("inlineData") or part.get("inline_data")
                    if inline_data and "data" in inline_data:
                        raw_bytes = base64.b64decode(inline_data["data"])
                        target_file.write_bytes(raw_bytes)
                        self.logger.info(f"Saved generated editorial image to {target_file}")
                        return target_file

                self.logger.warning("No inlineData image found in API response")
                return None
        except Exception as e:
            self.logger.warning(f"Image generation failed: {e}")
            return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_image_generator.py -v`
Expected: PASS

---

### Task 2: Update `src/sender.py` to Support Photo Post Delivery with Inline Button

**Files:**
- Modify: `src/sender.py`
- Modify: `tests/test_sender.py`

- [ ] **Step 1: Add unit tests for `send_article_with_photo` in `tests/test_sender.py`**

```python
@pytest.mark.asyncio
async def test_send_article_with_photo_success(mock_config, mock_logger, tmp_path):
    sender = DigestSender(mock_config, mock_logger)
    sender.bot = AsyncMock()

    test_img = tmp_path / "test.jpg"
    test_img.write_bytes(b"dummy image bytes")

    success = await sender.send_article_with_photo(
        title="Тестовый заголовок",
        lead="Тестовый лид статьи",
        telegraph_url="https://telegra.ph/test-article",
        photo_path=test_img,
    )

    assert success is True
    assert sender.bot.send_photo.called
    kwargs = sender.bot.send_photo.call_args.kwargs
    assert "Тестовый заголовок" in kwargs["caption"]
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_send_article_with_photo_falls_back_when_photo_missing(
    mock_config, mock_logger
):
    sender = DigestSender(mock_config, mock_logger)
    sender.bot = AsyncMock()
    sender.send_article_instant_view = AsyncMock(return_value=True)

    success = await sender.send_article_with_photo(
        title="Тестовый заголовок",
        lead="Тестовый лид статьи",
        telegraph_url="https://telegra.ph/test-article",
        photo_path=None,
    )

    assert success is True
    assert sender.send_article_instant_view.called
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_sender.py -k "test_send_article_with_photo" -v`
Expected: FAIL (`'DigestSender' object has no attribute 'send_article_with_photo'`)

- [ ] **Step 3: Implement `send_article_with_photo` in `src/sender.py`**

Add `InlineKeyboardButton`, `InlineKeyboardMarkup` import from `telegram` and implement:

```python
    async def send_article_with_photo(
        self,
        title: str,
        lead: str,
        telegraph_url: str,
        photo_path: Optional[Path | str] = None,
        user_id: Optional[int] = None,
    ) -> bool:
        """Send an editorial article photo post with headline, lead, and inline Telegraph button."""
        if user_id is None:
            user_id = self.target_user_id

        if user_id != self.target_user_id:
            self.logger.warning(f"Unauthorized send attempt to user {user_id}")
            return False

        destination_chat = self.target_chat_id or user_id

        # Fallback to instant view text message if photo is not provided or file doesn't exist
        if photo_path is None or not Path(photo_path).exists():
            self.logger.info("Photo path missing or invalid, falling back to text Instant View")
            return await self.send_article_instant_view(
                title=title, lead=lead, telegraph_url=telegraph_url, user_id=user_id
            )

        # Telegram photo caption limit is 1024 characters
        title_clean = title.strip()
        lead_clean = lead.strip()
        header = f"📰 *{title_clean}*\n\n"
        max_lead_chars = 1000 - len(header)
        if len(lead_clean) > max_lead_chars:
            lead_clean = lead_clean[:max_lead_chars].rsplit(" ", 1)[0] + "..."

        caption = f"{header}{lead_clean}" if lead_clean else f"📰 *{title_clean}*"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="⚡️ Читать статью полностью", url=telegraph_url)]]
        )

        try:
            with open(photo_path, "rb") as photo_file:
                await self.bot.send_photo(
                    chat_id=destination_chat,
                    photo=photo_file,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                )
            self.logger.info(f"✅ Sent article photo post to {destination_chat}")
            return True
        except TelegramError as e:
            if "Can't parse entities" in str(e):
                self.logger.warning("Markdown parse error in photo caption, trying plain text")
                try:
                    with open(photo_path, "rb") as photo_file:
                        plain_caption = f"📰 {title_clean}\n\n{lead_clean}" if lead_clean else f"📰 {title_clean}"
                        await self.bot.send_photo(
                            chat_id=destination_chat,
                            photo=photo_file,
                            caption=plain_caption,
                            parse_mode=None,
                            reply_markup=keyboard,
                        )
                    self.logger.info(f"✅ Sent article photo post (plain text caption) to {destination_chat}")
                    return True
                except TelegramError as e2:
                    self.logger.error(f"Failed to send photo post (plain text fallback): {e2}")
            self.logger.warning(f"Photo send failed ({e}), falling back to text Instant View")
            return await self.send_article_instant_view(
                title=title, lead=lead, telegraph_url=telegraph_url, user_id=user_id
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sender.py -k "test_send_article_with_photo" -v`
Expected: PASS

---

### Task 3: Integrate into `src/core.py` Workflow

**Files:**
- Modify: `src/core.py`
- Create / Update: `tests/test_article_image_workflow.py`

- [ ] **Step 1: Write integration unit tests in `tests/test_article_image_workflow.py`**

```python
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from src.core import generate_and_publish_article


@pytest.mark.asyncio
async def test_generate_and_publish_article_with_image(tmp_path):
    mock_config = MagicMock()
    mock_config.telegram_bot_token = "mock-bot-token"
    mock_config.settings.target_user_id = 12345
    mock_config.settings.target_chat_id = "@test_chat"
    mock_config.settings.article.enabled = True
    mock_config.settings.article.fallback_save_dir = str(tmp_path)
    mock_config.settings.article.telegraph_access_token = "mock-telegraph-token"
    mock_config.settings.article.author_name = "@test_author"

    fake_article = MagicMock()
    fake_article.title = "Тестовый заголовок"
    fake_article.lead = "Тестовый лид"
    fake_article.markdown_body = "Текст статьи..."

    with (
        patch("src.core.generate_article", new=AsyncMock(return_value=fake_article)),
        patch("src.core.TelegraphPublisher.create_page", new=AsyncMock(return_value="https://telegra.ph/mock-url")),
        patch("src.core.NewsImageGenerator.generate_prompt", new=AsyncMock(return_value="mock prompt")),
        patch("src.core.NewsImageGenerator.generate_image", new=AsyncMock(return_value=tmp_path / "mock.jpg")),
        patch("src.core.DigestSender.send_article_with_photo", new=AsyncMock(return_value=True)) as mock_send,
    ):
        mock_logger = MagicMock()
        success = await generate_and_publish_article(mock_config, mock_logger, dry_run=False)

        assert success is True
        assert mock_send.called
        kwargs = mock_send.call_args.kwargs
        assert kwargs["title"] == "Тестовый заголовок"
        assert kwargs["telegraph_url"] == "https://telegra.ph/mock-url"
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_article_image_workflow.py -v`
Expected: FAIL

- [ ] **Step 3: Update `src/core.py` `generate_and_publish_article`**

In `src/core.py`:
- Import `NewsImageGenerator` from `src.image_generator`.
- Before sending to Telegram:
  ```python
  image_generator = NewsImageGenerator(config, logger)
  image_prompt = await image_generator.generate_prompt(
      title=title,
      lead=lead,
      article_text=markdown_body,
  )
  photo_path = await image_generator.generate_image(prompt=image_prompt)

  sender = DigestSender(config, logger)
  success = await sender.send_article_with_photo(
      title=title,
      lead=lead,
      telegraph_url=telegraph_url,
      photo_path=photo_path,
      user_id=user_id,
  )
  ```

- [ ] **Step 4: Run full test suite to verify everything passes**

Run: `.venv/bin/pytest -v`
Expected: All tests pass.

---

### Task 4: End-to-End Verification

- [ ] **Step 1: Test with dry_run and local preview generation**
- [ ] **Step 2: Verify lint and formatting**

Run: `.venv/bin/ruff check .`
Expected: Clean check.
