"""AI Image generation module for news articles with multi-tier cascade."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from src.ai_providers import GoogleProvider, OpenAIProvider
from src.config_loader import Config

IMAGE_PROMPT_SYSTEM_INSTRUCTION = """You are a senior editorial art director for a local news outlet.
Your task is to convert a local news story (headline, lead, and details) into an optimal, high-fidelity English prompt for an AI image generation model (16:9 aspect ratio).

### 1. STYLE SELECTION
- **DEFAULT (90% of news):** "Realistic editorial photojournalism, authentic documentary photography, 35mm lens, candid street/indoor shot, natural lighting, gritty authentic textures, no studio gloss."
- **SATIRE / BUREAUCRATIC ABSURDITY (10% of news):** Only for bizarre rumors, absurd official announcements, or comedic city mishaps: "Expressive editorial satirical cartoon, colorful newspaper caricature style, expressive characters, vibrant ink and color wash."

### 2. REGIONAL AUTHENTICITY ANCHORS ({city_name}, Ukraine)
Anchor the scene to authentic Eastern European / Azov coastal urban reality:
- **Architecture:** 5-story Soviet-era brick or panel apartment buildings (khrushchevka), weathered balconies, metal entrance canopies, yellow gas pipes along exterior brick walls.
- **Courtyards & Streets:** Asphalt courtyards with patches, dry summer grass, poplars, acacia trees, domestic parked cars.
- **Domestic Interiors:** Authentic residential kitchens/apartments, tiled walls, simple countertops, portable LED flashlights, powerbanks, 5-liter transparent plastic water jugs with blue caps.
- **People:** Fictional local residents in authentic everyday casual clothing matching the season (summer: t-shirts, shorts, sandals; winter: jackets, beanies).

### 3. PROMPT SYNTAX STRUCTURE (Follow this exact order)
[Style & Camera Lens] + [Primary Subject & Action in Foreground] + [Authentic Setting & Background Details in {city_name}] + [Lighting Source & Atmosphere] + [Negative Quality Constraints].

### 4. STRICT NEGATIVE CONSTRAINTS (Crucial for realism)
Always conclude the prompt with:
"Unmarked blank facades, plain unbranded clothing, completely textless environment, NO text, NO letters, NO words on signs, NO English shopfronts, NO American suburban architecture, NO palm trees, NO watermarks, NO logos, NO artificial stock-photo gloss. Photorealistic 16:9 aspect ratio."

Output ONLY the final English prompt as a single cohesive paragraph without commentary, labels, or markdown formatting."""


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
    ) -> str:
        """Generate an English visual prompt for image generation models with multi-provider fallback."""
        city_en = _get_city_name_en(city_name)
        system_instruction = IMAGE_PROMPT_SYSTEM_INSTRUCTION.format(city_name=city_en)
        user_content = (
            f"Заголовок новости: {title}\n"
            f"Лид новости: {lead}\n"
            f"Краткий контекст статьи: {article_text[:1500]}\n\n"
            f"Сформируй один связный детальный промпт на английском языке для генерации изображения 16:9."
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
                    max_tokens=500,
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
        return (
            f"Realistic editorial photojournalism, documentary street photography. "
            f"A residential courtyard in {city_en}, Ukraine with Soviet-era brick apartment buildings. "
            f"STRICTLY NO text, NO letters, NO words, NO logos. 16:9 aspect ratio."
        )

    async def generate_image(
        self,
        prompt: str,
        output_dir: Optional[Path | str] = None,
        model_name: str = "gemini-3.1-flash-lite-image",
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
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
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

                    parts = candidates[0].get("content", {}).get("parts", [])
                    for part in parts:
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
            openrouter_payload = {
                "model": self.openrouter_image_model,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": ["image", "text"],
                "max_tokens": 2500,
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
