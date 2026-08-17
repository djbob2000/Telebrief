"""AI Image generation module for news articles."""

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from src.ai_providers import GoogleProvider
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
        city_en = _get_city_name_en(city_name)
        system_instruction = IMAGE_PROMPT_SYSTEM_INSTRUCTION.format(city_name=city_en)
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
                f"A residential courtyard in {city_en}, Ukraine with Soviet-era brick apartment buildings. "
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
            base_dir = getattr(self.config.settings.article, "fallback_save_dir", "data/articles")
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
