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

IMAGE_PROMPT_SYSTEM_INSTRUCTION = """You are a senior editorial art director for a local news outlet covering {city_name}, Ukraine.
Your task is to convert a local news story (headline, lead, and details) into an optimal, photorealistic, high-fidelity English prompt for an AI image generation model (16:9 aspect ratio).

### 1. EDITORIAL PHOTOJOURNALISM STYLE
- **Visual Goal:** Authentic documentary photography taken by a local photojournalist on a 35mm lens. Natural candid scene, natural realistic daylight or atmospheric dusk lighting, gritty authentic textures, zero stock-photo gloss.
- **Composition:** Focus on a grounded everyday moment with 1–3 fictional local residents or a tangible municipal setting.

### 2. REGIONAL AUTHENTICITY ANCHORS ({city_name}, Ukraine)
Anchor the scene strictly to authentic Eastern European / Azov coastal urban reality:
- **Architecture:** 5-story Soviet-era brick or panel residential apartment buildings (khrushchevka), weathered balconies, metal entrance canopies, yellow exterior gas pipes along brick facades.
- **Courtyards & Streets:** Asphalt courtyards with patches, dry summer grass, poplars, acacia trees, ordinary domestic cars.
- **Tangible Everyday Props (choose 2–3 relevant to the news):**
  * Utility/Blackout stories: A portable gasoline/diesel generator humming on the asphalt near a residential building entrance; people carrying 5-liter transparent plastic water jugs with blue handles; powerbanks and charging cables on a simple kitchen table; unlit apartment windows at dusk.
  * Fire/Emergency stories: Firefighters near a red fire truck extinguishing dry grass/bushes on a hill; distant smoke over trees.
  * Everyday City stories: Quiet bus stops, municipal repair crews, local grocery courtyards.
- **People:** Everyday ordinary local residents in authentic casual summer clothing (plain t-shirts, shorts, sandals, light shirts). Calm, natural human behavior.

### 3. CATEGORICAL NEGATIVE CONSTRAINTS (Crucial)
You MUST strictly exclude:
- **NO PROTESTS / NO RALLIES:** NEVER depict protests, demonstrations, crowds holding signs, cardboard placards, banners, or activists.
- **NO TEXT / NO SIGNS:** Absolutely NO text, NO letters, NO words on signs, NO neon signs, NO shopfront names, NO English or foreign text, NO logos on clothing.
- **NO FOREIGN SETTINGS:** NO Italian or Western European cobblestone streets, NO British double-decker buses, NO Western police uniforms, NO American suburban architecture, NO palm trees.
- **NO FAKE DISASTERS:** NO explosions, NO blood, NO weapons.

### 4. PROMPT STRUCTURE
Combine into a single cohesive English paragraph (approx 80–120 words):
[Optics & Documentary Style] + [1–2 Everyday Subjects Performing Specific Action from the News] + [Authentic Setting in {city_name}: Soviet-era 5-story brick building, asphalt courtyard, poplars] + [Tangible News Props: e.g. generator on asphalt / water jugs / powerbanks] + [Lighting & Atmosphere] + [Negative Constraints: completely textless, blank facades, NO signs, NO text, NO protests, photorealistic 16:9].

Output ONLY the final English prompt as a single cohesive paragraph without commentary, labels, quotes, or markdown."""


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
            f"Краткий контекст статьи: {article_text[:2000]}\n\n"
            f"Сформируй один связный детальный промпт на английском языке для генерации фотореалистичной иллюстрации 16:9 без текста и без плакатов."
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
                    max_tokens=4096,
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
            f"Realistic editorial photojournalism, documentary street photography, 35mm lens. "
            f"A quiet residential courtyard in {city_en}, Ukraine with 5-story Soviet-era brick apartment buildings, "
            f"a portable generator standing on the asphalt path, people carrying water jugs in summer clothing. "
            f"Completely textless environment, unmarked facades, NO text, NO letters, NO words, NO signs, NO posters. 16:9 aspect ratio."
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
                "max_tokens": 4096,
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
