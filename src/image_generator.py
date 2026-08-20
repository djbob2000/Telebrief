"""AI Image generation module for news articles with multi-tier cascade."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from src.ai_providers import GoogleProvider, OpenAIProvider
from src.config_loader import Config

IMAGE_PROMPT_SYSTEM_INSTRUCTION = """You are a senior editorial art director for a local news outlet covering {city_name}, Ukraine.
Your task is to convert a local news story (headline, lead, and context) into a single, cohesive, photorealistic English visual prompt (16:9 aspect ratio) for an AI image generator.

### 1. EDITORIAL PHOTOJOURNALISM AESTHETICS (Top Priority)
- **Single Continuous Frame:** EXACTLY ONE wide continuous horizontal photograph (16:9). Full-bleed view. Never generate a split screen, diptych, collage, film strip, multi-panel layout, or colored dividing bars.
- **Optics & Lighting:** 35mm documentary street photography, eye-level medium or wide environmental shot, natural daylight or twilight, realistic film grain, zero studio lighting, zero stock-photo gloss.
- **Composition:** Natural candid scene with subjects naturally embedded in the city environment. No posed studio portraits or fake smiles.

### 2. AUTHENTIC REGIONAL ANCHORS ({city_name}, Ukraine)
Anchor outdoor and indoor scenes strictly to Eastern European / Azov coastal reality:
- **Architecture:** Weathered 5-story Soviet silicate brick or panel buildings (khrushchevka), glazed balconies, metal entrance canopies, exterior yellow gas facade pipes.
- **Environment:** Patchwork cracked asphalt, tall poplars, acacia bushes, dusty curbs, ordinary domestic vehicles.
- **Seasonal Context:**
  * Warm season: Casual t-shirts, light shirts, dry grass, dusty green trees.
  * Cold season: Dark puffer jackets, warm beanies, boots, overcast gray sky, wet asphalt, bare trees.
- **Everyday Props (select 2–3 items):**
  * Blackouts/Utilities: Portable humming generator on asphalt near entrance, 5-liter plastic water jugs with blue handles, powerbanks on a wooden table.
  * Municipal/Emergency: Utility repair crew in work overalls, emergency repair van, fire truck.
  * Community: Courtyard benches, bus stops, local grocery entrances.
- **Subjects:** 1–3 fictional local residents in plain everyday wear behaving calmly and naturally.

### 3. STRICT NEGATIVE CONSTRAINTS
- **NO COLLAGES / NO DIVIDERS:** Strictly one single frame. NO collage, NO split screen, NO diptych, NO multiple panels, NO film strips, NO solid color bars, NO separator banners, NO borders.
- **NO PROTESTS:** NEVER depict rallies, demonstrations, placards, or banners.
- **CLEAN SURFACES (NO TEXT):** Blank facades, plain unbranded clothing, completely textless scene, NO words, NO letters, NO signs.
- **NO FOREIGN CLICHÉS:** NO Western European cobblestones, NO American suburban houses, NO palm trees.
- **NO FAKE DISASTERS:** NO cinematic explosions, NO blood, NO weapons.

### 4. OUTPUT FORMAT & STRUCTURE
Output EXACTLY ONE cohesive English paragraph (75–100 words) starting directly with the format anchor:
"A single horizontal 16:9 documentary editorial photograph, single continuous frame, 35mm lens, candid shot of [1–2 Everyday Subjects Performing Specific Action] in {city_name}, Ukraine. [Authentic setting: 5-story brick khrushchevka, cracked asphalt courtyard, poplars]. [Story props]. [Atmospheric natural lighting and season]. Clean blank unmarked facades, plain unbranded clothing, completely textless scene, no collage, no split screen, no color bars, photorealistic 16:9."

Do NOT include labels, quotes, explanations, markdown formatting, or multiple paragraphs."""

IMAGE_REDRAW_SYSTEM_INSTRUCTION = """You are a senior editorial art director for a local news outlet covering {city_name}, Ukraine.
Your task is to formulate a precise English visual prompt (16:9 aspect ratio) that instructs an AI image model to cleanly redraw/re-imagine an attached reference news photograph.

### 1. EDITORIAL REDRAW GUIDELINES & DIRECTIVES
- **Single Continuous Frame (Top Priority):** Output MUST be EXACTLY ONE single continuous horizontal photograph (16:9), full-bleed. Do NOT create comparison shots, side-by-side views, before-and-after panels, split screens, diptychs, film strips, or colored divider bars.
- **Scene Transformation:** Re-create the core action, spatial layout, and subjects from the reference photograph as a crisp, authentic 35mm documentary editorial photo.
- **Artifact & Clutter Removal:** Eliminate all watermarks, channel logos, timestamps, pixelation, and camera artifacts.
- **Regional Authenticity ({city_name}):** Eastern European architecture (weathered Soviet brick/panel buildings, asphalt courtyards, local poplars/acacias, seasonal attire).
- **Strict Textless Environment:** Blank facades, plain clothing, textless scene, NO watermarks, NO logos, NO signs, NO text.

### 2. OUTPUT FORMAT & STRUCTURE
Output EXACTLY ONE cohesive English paragraph (75–100 words) starting directly with the format anchor:
"A single horizontal 16:9 documentary editorial photograph, single continuous frame, 35mm lens, recreating the news scene in {city_name}, Ukraine with authentic composition. [Detailed description of subjects, realistic Soviet-era residential setting, tangible props, and natural lighting]. High-fidelity rendering with clean unmarked surfaces, plain clothing, completely textless scene, no watermarks, no logos, no collage, no split screen, no dividing bars, photorealistic 16:9."

Do NOT include labels, quotes, explanations, markdown formatting, or multiple paragraphs."""


CITY_NAMES_EN: dict[str, str] = {
    "Бердянск": "Berdyansk",
    "Бердянськ": "Berdiansk",
    "Мелитополь": "Melitopol",
    "Мелітополь": "Melitopol",
}


def _get_city_name_en(city_name: str) -> str:
    return CITY_NAMES_EN.get(city_name.strip(), city_name.strip())


def _extract_openrouter_image_bytes(data: dict) -> Optional[bytes]:
    """Extract raw image bytes from OpenRouter chat completion response."""
    choices = data.get("choices", [])
    if not choices:
        return None
    msg = choices[0].get("message", {})
    images = msg.get("images", [])
    if images:
        img_item = images[0]
        if isinstance(img_item, dict):
            url_val = img_item.get("image_url", {})
            if isinstance(url_val, dict):
                url_str = url_val.get("url", "")
            else:
                url_str = str(url_val)
            if "," in url_str:
                url_str = url_str.split(",", 1)[1]
            if url_str:
                return base64.b64decode(url_str)
        elif isinstance(img_item, str):
            if "," in img_item:
                img_item = img_item.split(",", 1)[1]
            return base64.b64decode(img_item)
    content = msg.get("content")
    if isinstance(content, str) and content.startswith("data:image/"):
        b64_str = content.split(",", 1)[1]
        return base64.b64decode(b64_str)
    return None


class NewsImageGenerator:
    """Generates editorial illustrations for news articles using a 3-tier cascade."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # Gather all configured Gemini API keys (Tier 1, Tier 2, Tier 3)
        self.gemini_keys: list[str] = [
            k
            for k in [
                getattr(config, "google_api_key", ""),
                getattr(config, "google_api_key_2", ""),
                getattr(config, "google_api_key_3", ""),
            ]
            if k
        ]

        # OpenRouter fallback settings
        self.openrouter_api_key = getattr(config, "openrouter_api_key", "")
        self.openrouter_base_url = (
            getattr(config, "openrouter_base_url", "") or "https://openrouter.ai/api/v1"
        )
        self.openrouter_model = getattr(config, "openrouter_model", "") or "openrouter/free"
        self.openrouter_image_model = (
            getattr(config, "openrouter_image_model", "") or "google/gemini-3.1-flash-lite-image"
        )

        # Build prompt generation providers cascade
        self.prompt_providers: list[tuple[str, GoogleProvider | OpenAIProvider, str]] = []
        for idx, key in enumerate(self.gemini_keys, start=1):
            self.prompt_providers.append(
                (
                    f"google-{idx}",
                    GoogleProvider(api_key=key, logger=logger),
                    getattr(config.settings, "ai_model", "gemini-3.7-flash"),
                )
            )
        if self.openrouter_api_key:
            self.prompt_providers.append(
                (
                    "openrouter-text",
                    OpenAIProvider(
                        api_key=self.openrouter_api_key,
                        logger=logger,
                        base_url=self.openrouter_base_url,
                    ),
                    self.openrouter_model,
                )
            )

    @property
    def api_key(self) -> Optional[str]:
        """Backward compatibility for primary Gemini key."""
        return self.gemini_keys[0] if self.gemini_keys else None

    async def generate_prompt(
        self,
        title: str,
        lead: str,
        article_text: str,
        city_name: str = "Бердянск",
        has_reference_image: bool = False,
    ) -> str:
        """Generate an English visual prompt for image generation models with multi-provider fallback."""
        city_en = _get_city_name_en(city_name)
        if has_reference_image:
            system_instruction = IMAGE_REDRAW_SYSTEM_INSTRUCTION.format(city_name=city_en)
            user_content = (
                f"Заголовок новости: {title}\n"
                f"Лид новости: {lead}\n"
                f"Краткий контекст статьи: {article_text[:2000]}\n\n"
                f"Сформируй один связный детальный промпт на английском языке для чистой перерисовки фотографии к этой новости в виде единого непрерывного кадра 16:9 (single continuous photograph, no collage, no split screen) без водяных знаков, логотипов, букв и текста."
            )
        else:
            system_instruction = IMAGE_PROMPT_SYSTEM_INSTRUCTION.format(city_name=city_en)
            user_content = (
                f"Заголовок новости: {title}\n"
                f"Лид новости: {lead}\n"
                f"Краткий контекст статьи: {article_text[:2000]}\n\n"
                f"Сформируй один связный детальный промпт на английском языке для генерации фотореалистичной иллюстрации в виде единого кадра 16:9 (single continuous photograph, no collage, no split screen) без текста и без плакатов."
            )

        for label, provider, model in self.prompt_providers:
            try:
                response = await provider.chat_completion(
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": user_content},
                    ],
                    model=model,
                    temperature=0.7,
                    max_tokens=8192,
                )
                clean_prompt = response.strip().strip('"').strip("'")
                if clean_prompt:
                    self.logger.info("Generated visual prompt for article using slot %s", label)
                    return clean_prompt
            except Exception as e:
                self.logger.warning(
                    "Prompt generation slot %s failed: %s; trying next slot", label, e
                )

        self.logger.warning("All prompt generation slots failed, using static fallback prompt.")
        if has_reference_image:
            return (
                f"Realistic editorial photojournalism, documentary street photography, 35mm lens, single unified frame. "
                f"A clean high-resolution re-imagining of the news scene in {city_en}, Ukraine based on the reference photo, "
                f"authentic composition and local environment. Completely textless, blank facades, NO text, NO letters, NO words, NO signs, NO watermarks, NO logos, NO collage, NO split screen, NO diptych. 16:9 aspect ratio."
            )
        return (
            f"Realistic editorial photojournalism, documentary street photography, 35mm lens, single unified frame. "
            f"A quiet residential courtyard in {city_en}, Ukraine with 5-story Soviet-era brick apartment buildings, "
            f"a portable generator standing on the asphalt path, people carrying water jugs in summer clothing. "
            f"Completely textless environment, unmarked facades, NO text, NO letters, NO words, NO signs, NO posters, NO collage, NO split screen, NO diptych. 16:9 aspect ratio."
        )

    async def generate_image(
        self,
        prompt: str,
        output_dir: Optional[Path | str] = None,
        model_name: str = "gemini-3.1-flash-lite-image",
        reference_image_bytes: Optional[bytes] = None,
    ) -> Optional[Path]:
        """Generate image using 3-tier cascade (Google Key 1 -> Google Key 2 -> OpenRouter) and save to disk."""
        if output_dir is None:
            base_dir = getattr(self.config.settings.article, "fallback_save_dir", "data/articles")
            output_dir = Path(base_dir) / "images"
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_file = output_dir / f"editorial_{timestamp}.jpg"

        # Tier 1 & 2: Try direct Google Gemini API keys
        for idx, key in enumerate(self.gemini_keys, start=1):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
            parts: list[dict[str, Any]] = []
            if reference_image_bytes:
                b64_ref = base64.b64encode(reference_image_bytes).decode("utf-8")
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": b64_ref,
                        }
                    }
                )
            parts.append({"text": prompt})

            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            }
            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code != 200:
                        self.logger.warning(
                            "Google image slot %d failed (HTTP %s): %s; trying next slot",
                            idx,
                            resp.status_code,
                            resp.text[:200],
                        )
                        continue

                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        self.logger.warning("Google image slot %d returned no candidates", idx)
                        continue

                    parts_resp = candidates[0].get("content", {}).get("parts", [])
                    for part in parts_resp:
                        inline_data = part.get("inlineData") or part.get("inline_data")
                        if inline_data and "data" in inline_data:
                            raw_bytes = base64.b64decode(inline_data["data"])
                            target_file.write_bytes(raw_bytes)
                            self.logger.info(
                                "Saved generated editorial image (via Google slot %d) to %s",
                                idx,
                                target_file,
                            )
                            return target_file
            except Exception as exc:
                self.logger.warning(
                    "Google image slot %d exception: %s; trying next slot", idx, exc
                )

        # Tier 3: OpenRouter Image Generation Fallback
        if self.openrouter_api_key:
            openrouter_url = f"{self.openrouter_base_url.rstrip('/')}/chat/completions"
            msg_content: str | list[dict[str, Any]]
            if reference_image_bytes:
                b64_ref = base64.b64encode(reference_image_bytes).decode("utf-8")
                msg_content = [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_ref}"},
                    },
                    {"type": "text", "text": prompt},
                ]
            else:
                msg_content = prompt

            openrouter_payload = {
                "model": self.openrouter_image_model,
                "messages": [{"role": "user", "content": msg_content}],
                "modalities": ["image", "text"],
                "max_tokens": 8192,
            }
            headers = {
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/telebrief",
                "X-Title": "Telebrief",
            }
            try:
                self.logger.info(
                    "Trying OpenRouter image slot with model %s", self.openrouter_image_model
                )
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        openrouter_url, json=openrouter_payload, headers=headers
                    )
                    if resp.status_code != 200:
                        self.logger.warning(
                            "OpenRouter image generation returned HTTP %s: %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                    else:
                        openrouter_bytes = _extract_openrouter_image_bytes(resp.json())
                        if openrouter_bytes:
                            target_file.write_bytes(openrouter_bytes)
                            self.logger.info(
                                "Saved generated editorial image (via OpenRouter %s) to %s",
                                self.openrouter_image_model,
                                target_file,
                            )
                            return target_file
                        else:
                            self.logger.warning(
                                "OpenRouter response did not contain extractable image bytes"
                            )
            except Exception as exc:
                self.logger.warning("OpenRouter image generation exception: %s", exc)

        self.logger.warning("All image generation slots exhausted; returning None")
        return None
