# Design Specification: News Editorial Image Generation & Telegram Photo Publishing

## 1. Context & Motivation

When publishing long-form news articles to Telegram channels via Telegra.ph:
- Standard text link previews (`disable_web_page_preview=False`) duplicate the headline and lead text, display raw URLs, and look cluttered.
- Visual engagement (CTR) in Telegram is significantly higher when stories are delivered as **Photo Posts** (Format B): an authentic, relatable editorial image with the headline and lead in the photo caption, followed by an **Inline Keyboard Button** (`[ ⚡ Читать статью полностью ]`) linking directly to the full Telegra.ph article.
- Because local news channels lack photographers on the ground, images must be generated on-demand using AI based on verified article text.
- Public uploads to `telegra.ph/upload` have been discontinued by Telegram, so images are sent directly to Telegram using standard Telegram Bot API `sendPhoto`, where Telegram securely hosts and serves them on its high-speed CDN.

---

## 2. Architecture & Component Flow

```
[News Article Generated: Title + Lead + Markdown Text]
                       │
                       ▼
        [src/image_generator.py: NewsImageGenerator]
        │
        ├── 1. generate_visual_prompt(title, lead, text, city="Бердянск")
        │      └── Calls active LLM (Gemini 3.7 Flash) using news-image-redraw rules
        │          Produces detailed English prompt with local geographic & architectural anchors
        │
        └── 2. generate_image(prompt, output_dir)
               └── Calls Google Gemini Image API (gemini-3.1-flash-lite-image)
                   Saves JPEG image to data/articles/images/{timestamp}_editorial.jpg
                       │
                       ▼
        [src/telegraph.py: TelegraphPublisher]
        └── Publishes formatted markdown body to Telegra.ph → receives telegraph_url
                       │
                       ▼
        [src/sender.py: DigestSender.send_article_with_photo]
        └── Sends Telegram Bot API send_photo:
            - photo: local file stream
            - caption: 📰 *{title}*\n\n{lead} (truncated safely to <= 1024 chars)
            - reply_markup: InlineKeyboardMarkup with button [ ⚡ Читать статью полностью ]
            - fallback: if photo generation/sending fails, falls back to text Instant View
```

---

## 3. Detailed Component Design

### 3.1 `src/image_generator.py` (New Module)

**Class: `NewsImageGenerator`**

* **`__init__(self, config: Config, logger: logging.Logger, ai_provider: AIProvider | None = None)`**:
  * Loads API keys (`GEMINI_API_KEY`, backup keys) and sets output directory (`data/articles/images`).
* **`async def generate_prompt(self, title: str, lead: str, article_text: str, city_name: str = "Бердянск") -> str`**:
  * Formulates the LLM prompt using guidelines from `.agents/skills/news-image-redraw/SKILL.md`.
  * Enforces primary style: *Realistic Editorial Photojournalism* (or *Editorial Cartoon* if satirical/bureaucratic).
  * Injects mandatory local architectural anchors (e.g., `Soviet-era 5-story brick and panel residential apartment blocks`, `courtyard with poplars and acacias`, `NO English signage, NO American architecture, NO text on signs`).
  * Returns English prompt string.
* **`async def generate_image(self, prompt: str, output_dir: Path | str | None = None) -> Path | None`**:
  * Sends POST request to Google Gemini Image API (`https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-image:generateContent?key={API_KEY}`).
  * Extracts base64 image data from `candidates[0].content.parts`.
  * Saves image to disk (e.g. `data/articles/images/article_YYYYMMDD_HHMMSS.jpg`).
  * Returns the `Path` to the saved image, or `None` if quota/rate-limited/failed.

### 3.2 `src/sender.py` (Enhancements)

* **`async def send_article_with_photo(self, title: str, lead: str, telegraph_url: str, photo_path: Path | str | None = None, user_id: int | None = None) -> bool`**:
  * Checks if `photo_path` exists and is readable.
  * Formats caption: `📰 *{title.strip()}*\n\n{lead.strip()}`.
  * Truncates caption if length exceeds Telegram photo caption limit (1024 characters) at nearest sentence boundary.
  * Builds `InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚡️ Читать статью полностью", url=telegraph_url)]])`.
  * Invokes `await self.bot.send_photo(...)`.
  * Handles fallback: if `photo_path` is None or send fails, gracefully falls back to `send_article_instant_view(title, lead, telegraph_url, user_id)`.

### 3.3 `src/core.py` (Workflow Integration)

* In `generate_and_publish_article`:
  * After editorial writer produces `title`, `lead`, and `markdown_body`:
  * Initialize `NewsImageGenerator`.
  * Call `prompt = await image_generator.generate_prompt(title, lead, markdown_body)`.
  * Call `photo_path = await image_generator.generate_image(prompt)`.
  * Publish article to Telegra.ph to obtain `telegraph_url`.
  * Call `await sender.send_article_with_photo(title=title, lead=lead, telegraph_url=telegraph_url, photo_path=photo_path, user_id=user_id)`.

---

## 4. Error Handling & Edge Cases

1. **Image Generation Failure / 429 Quota Exceeded:**
   * Log warning; do not fail article publication.
   * Seamlessly fallback to sending text Instant View message with button/link.
2. **Caption Length Limit (1024 chars):**
   * Telegram strictly enforces 1024 chars for photo captions.
   * `send_article_with_photo` trims `lead` to fit under 1000 chars while preserving markdown formatting.
3. **Markdown Parse Errors:**
   * If Telegram throws `Can't parse entities` error on photo caption, retry sending photo with `parse_mode=None` (plain text).

---

## 5. Verification Plan

* **Unit Tests (`tests/test_image_generator.py`):**
  * Test prompt generation with mocked LLM response.
  * Test image response parsing (extracting base64 payload and saving file).
  * Test graceful handling of API errors (429, 500, network timeouts).
* **Unit Tests (`tests/test_sender.py`):**
  * Test `send_article_with_photo` success with mock Bot.
  * Test caption length truncation (> 1024 chars).
  * Test fallback to text message when photo is missing or fails.
* **End-to-End Dry Run:**
  * Run test publication workflow against `preview_2026-08-16_14-04-04_editorial.md`.
