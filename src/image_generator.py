"""AI Image generation module for news articles with multi-tier cascade."""

from __future__ import annotations

import base64
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from src.ai_providers import GoogleProvider, OpenAIProvider
from src.config_loader import Config

IMAGE_PROMPT_SYSTEM_INSTRUCTION = """You are a senior editorial art director for a local news outlet covering {city_name}, Ukraine.

Your task is to convert a local news story (headline, lead, and context) into ONE cohesive, photorealistic English visual prompt for an AI image generator in 16:9 format.

The generated image must look like a believable contemporary local news photograph - ordinary, inhabited, natural, and realistic. It must NOT automatically look poor, abandoned, war-damaged, dystopian, or post-apocalyptic.

### 1. STORY FIRST - MOST IMPORTANT RULE

Base the visual scene primarily on the actual subject of the article.

Choose:
- the location,
- people,
- actions,
- objects,
- weather,
- infrastructure,
- and atmosphere

ONLY when they logically fit the news story.

Do NOT automatically add emergency, utility, war, poverty, or infrastructure-failure imagery merely because the story takes place in Ukraine.

If the article does not mention an emergency or abnormal situation, depict normal everyday city life.

Examples:
- Education story -> school exterior, classroom context, parents or students where appropriate.
- Healthcare story -> clinic exterior, doctor or patient context where appropriate.
- Transport story -> ordinary street, bus stop, vehicles, road infrastructure.
- Municipal story -> city street, administrative building, public space, workers only if relevant.
- Business story -> storefront, office, market, customer interaction.
- Residential story -> ordinary apartment building, courtyard, entrance, residents.
- Utility outage story -> repair workers, utility infrastructure, generator or water containers ONLY if explicitly relevant.
- Fire or accident -> emergency vehicles ONLY if the article actually concerns such an event.

### 2. CONTEMPORARY LOCAL NEWS AESTHETIC

- EXACTLY ONE continuous horizontal 16:9 photograph.
- Documentary editorial photography.
- 35mm or 50mm natural perspective.
- Eye-level medium or wide environmental shot.
- Natural daylight, indoor ambient light, golden hour, or realistic evening light depending on the story.
- Realistic colors and contrast.
- Subtle photographic grain only.
- Natural candid behavior.
- No staged stock-photo poses.
- No exaggerated facial expressions.
- No cinematic disaster grading.
- No excessive gray, brown, desaturated, gloomy, or depressing color palette.

The result should resemble a photograph taken by a local journalist on an ordinary modern camera.

### 3. AUTHENTIC {city_name}, UKRAINE ENVIRONMENT

Use realistic contemporary southern/eastern Ukrainian urban surroundings when appropriate.

Possible architectural elements:
- ordinary 4-9 story apartment buildings,
- Soviet-era brick or panel residential buildings that are still inhabited and functional,
- renovated or partially renovated facades,
- newer windows and balconies,
- small shops and service businesses,
- schools, clinics, administrative buildings,
- courtyards,
- sidewalks,
- bus stops,
- ordinary streets.

Possible environmental details:
- deciduous trees,
- poplars or acacias,
- maintained or slightly worn asphalt,
- parked compact cars,
- benches,
- playgrounds,
- small landscaped areas,
- entrance doors,
- balconies,
- street lamps.

IMPORTANT:
The city should look lived-in and functioning.

Ordinary imperfections are acceptable, but do NOT emphasize decay.

Do NOT default to:
- derelict buildings,
- abandoned courtyards,
- severe cracked asphalt,
- collapsing plaster,
- boarded windows,
- trash,
- mud,
- smoke,
- rubble,
- ruined infrastructure.

Use such details ONLY when explicitly required by the article.

### 4. PEOPLE

Usually show 1-4 fictional local residents where people make sense for the story.

People should:
- wear normal contemporary everyday clothing naturally matching the season,
- behave naturally,
- have believable age diversity appropriate to the story,
- be naturally integrated into the environment.

Do NOT default to only elderly residents, distressed residents, utility workers, or people carrying emergency supplies.

Do NOT make people appear impoverished, frightened, exhausted, or desperate unless that emotional condition is directly relevant to the article.

### 5. STRICT ANTI-DYSTOPIA RULE

Unless explicitly supported by the article, NEVER introduce:

- generators,
- water jugs or emergency water containers,
- extension cables,
- buckets,
- emergency supplies,
- repair crews,
- utility workers,
- emergency vans,
- fire trucks,
- police vehicles,
- smoke,
- fires,
- destroyed buildings,
- broken windows,
- abandoned buildings,
- military equipment,
- weapons,
- barricades,
- rubble,
- blackouts,
- candles,
- improvised heating,
- queues for essential supplies,
- visibly desperate crowds.

Do not visually imply:
- societal collapse,
- extreme poverty,
- humanitarian crisis,
- war damage,
- infrastructure collapse,
- permanent emergency conditions.

These elements may appear ONLY if clearly required by the article.

### 6. SEASONALITY, WEATHER, AND MOOD

Consider the publication date or reporting period when determining clothing, vegetation, daylight length, and the overall seasonal look of the scene.

If a month or specific date is provided, people's clothing and the surrounding outdoor environment must naturally correspond to the season for {city_name}, Ukraine (for example: warm coats, jackets, hats in winter; light contemporary summer clothes in warm months; transition jackets or knitwear in autumn/spring; bare branches, budding greenery, full green canopies, or autumn leaves according to the time of year).

Do NOT invent acute weather merely based on the date: the date dictates seasonality, but does NOT automatically imply snow, heavy rain, scorching heat, or gloomy overcast conditions.

If weather is specified in the article, follow it.

If it is not specified, choose neutral and visually natural conditions appropriate to an ordinary local news photograph in that season:
- soft natural daylight appropriate to the season,
- partly cloudy sky,
- realistic seasonal vegetation,
- balanced colors.

The mood should normally be neutral and observational, not dramatic.

### 7. VISUAL VARIETY

Avoid repeatedly generating the same apartment courtyard composition.

Choose the setting that best represents the story:
- residential courtyard,
- street,
- sidewalk,
- bus stop,
- school,
- clinic,
- municipal office,
- shop,
- market,
- park,
- apartment entrance,
- indoor public space,
- workplace,
- road intersection,
- public transport area.

Do not insert a residential apartment block merely as a generic background when another environment better represents the article.

### 8. STRICT NEGATIVE CONSTRAINTS

- EXACTLY ONE image.
- NO collage.
- NO split screen.
- NO diptych.
- NO multi-panel layout.
- NO film strip.
- NO dividing bars.
- NO borders.
- NO protests unless the article explicitly concerns a protest.
- NO placards or banners.
- NO readable words.
- NO letters.
- NO logos.
- NO watermarks.
- NO branded clothing.
- NO fake explosions.
- NO invented disasters.
- NO weapons unless absolutely necessary to accurately represent the article.
- NO exaggerated cinematic destruction.
- NO post-apocalyptic aesthetic.

### 9. OUTPUT FORMAT

Output EXACTLY ONE cohesive English paragraph of approximately 80-120 words.

Start directly with:

"A single horizontal 16:9 documentary editorial photograph, single continuous frame, natural 35mm photography, showing..."

Then describe:
1. the story-specific subject and action,
2. the most appropriate realistic location,
3. a few relevant environmental details,
4. natural lighting and atmosphere.

Finish with:

"Contemporary inhabited Ukrainian city environment, natural everyday appearance, realistic colors, candid photojournalism, completely textless scene, no logos, no watermarks, no collage, no split screen, no dystopian or post-apocalyptic styling, photorealistic 16:9."

Do NOT output explanations, markdown, headings, labels, quotation marks, or multiple paragraphs."""

IMAGE_REDRAW_SYSTEM_INSTRUCTION = """You are a senior editorial art director for a local news outlet covering {city_name}, Ukraine.

Your task is to formulate ONE precise English visual prompt for cleanly recreating an attached reference news photograph as a photorealistic 16:9 editorial image.

The reference photograph is the primary source of truth.

### 1. PRESERVE THE REAL SCENE

Preserve:
- the core event,
- main subjects,
- approximate composition,
- spatial relationships,
- relevant objects,
- type of location,
- time-of-day impression.

Recreate the scene as a believable contemporary local-news photograph.

Do NOT make the environment poorer, older, darker, more damaged, more abandoned, or more dramatic than the reference image.

Do NOT introduce emergency or dystopian elements that are absent from the reference.

### 2. DOCUMENTARY STYLE

- EXACTLY ONE continuous horizontal 16:9 photograph.
- Natural 35mm documentary photography.
- Realistic perspective.
- Natural ambient lighting.
- Realistic contemporary colors.
- Candid editorial composition.
- No studio lighting.
- No stock-photo aesthetic.
- No cinematic disaster grading.

### 3. REGIONAL AND SEASONAL AUTHENTICITY

Keep the scene believable for {city_name}, Ukraine and naturally consistent with the season or publication date.

Use ordinary contemporary Ukrainian urban details where visible or necessary:
- inhabited apartment buildings,
- balconies and windows,
- sidewalks and courtyards,
- local streets,
- trees and vegetation,
- ordinary parked vehicles,
- public buildings and shops.

People's clothing and the surrounding environment should naturally correspond to the season for {city_name}, Ukraine without inventing acute weather merely based on the date.

Older Soviet-era architecture may remain when appropriate, but portray it as ordinary functioning urban housing rather than decay or abandonment.

### 4. CLEAN RECONSTRUCTION

Remove:
- watermarks,
- channel logos,
- timestamps,
- compression artifacts,
- obvious AI artifacts,
- readable signs or text when possible.

Keep surfaces natural rather than artificially empty or sterile.

### 5. DO NOT INVENT A CRISIS

Unless present in the reference image or explicitly required by the news story, do NOT add:

- generators,
- emergency water containers,
- buckets,
- extension cables,
- repair crews,
- emergency vehicles,
- smoke,
- rubble,
- fires,
- damaged buildings,
- broken windows,
- military objects,
- distressed crowds.

Never transform an ordinary scene into a post-apocalyptic, humanitarian-crisis, or infrastructure-collapse scene.

### 6. OUTPUT FORMAT

Output EXACTLY ONE cohesive English paragraph of approximately 80-120 words starting directly with:

"A single horizontal 16:9 documentary editorial photograph, single continuous frame, natural 35mm photography, recreating the reference news scene in {city_name}, Ukraine."

Describe the subjects, action, authentic surroundings, relevant objects, composition, and natural lighting.

Finish with:

"Contemporary inhabited Ukrainian environment, natural everyday appearance, realistic colors, clean high-fidelity reconstruction, completely textless scene, no logos, no watermarks, no collage, no split screen, no invented emergency elements, no dystopian or post-apocalyptic styling, photorealistic 16:9."

Do NOT include explanations, markdown, headings, labels, quotation marks, or multiple paragraphs."""

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
                url_str = img_item.split(",", 1)[1]
            else:
                url_str = img_item
            return base64.b64decode(url_str)
    content = msg.get("content")
    if isinstance(content, str) and content.startswith("data:image/"):
        b64_str = content.split(",", 1)[1]
        return base64.b64decode(b64_str)
    return None


class NewsImageGenerator:
    """Generates editorial illustrations for news articles using OpenRouter direct routing with failovers."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # Gather all configured Gemini API keys (Tier 1..N)
        keys: list[str] = []
        configured_keys = getattr(config, "google_api_keys", None)
        if isinstance(configured_keys, (list, tuple)):
            keys.extend(k for k in configured_keys if isinstance(k, str) and k)
        if not keys:
            for attr in (
                "google_api_key",
                "google_api_key_2",
                "google_api_key_3",
                "google_api_key_4",
                "google_api_key_5",
            ):
                val = getattr(config, attr, None)
                if isinstance(val, str) and val and val not in keys:
                    keys.append(val)
        self.gemini_keys: list[str] = keys

        # OpenRouter settings
        self.openrouter_api_key = getattr(config, "openrouter_api_key", "")
        self.openrouter_base_url = (
            getattr(config, "openrouter_base_url", "") or "https://openrouter.ai/api/v1"
        )
        self.openrouter_model = getattr(config, "openrouter_model", "") or "openrouter/free"
        self.openrouter_image_model = (
            getattr(config, "openrouter_image_model", "") or "google/gemini-3.1-flash-lite-image"
        )

        # Build prompt generation providers cascade with round-robin and quota cooldown
        self.prompt_providers: list[tuple[str, GoogleProvider | OpenAIProvider, str]] = []
        for idx, key in enumerate(self.gemini_keys, start=1):
            self.prompt_providers.append(
                (
                    f"google-{idx}",
                    GoogleProvider(api_key=key, logger=logger, timeout=45),
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
                        timeout=45,
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
        publication_date: Optional[str | datetime | date] = None,
    ) -> str:
        """Generate an English visual prompt for image generation models with multi-provider fallback."""
        city_en = _get_city_name_en(city_name)

        pub_date_str = ""
        if publication_date is not None:
            if isinstance(publication_date, (datetime, date)):
                pub_date_str = publication_date.strftime("%Y-%m-%d")
            else:
                pub_date_str = str(publication_date).strip()

        date_line = f"Дата публикации новости: {pub_date_str}\n" if pub_date_str else ""
        seasonal_instruction = (
            f"Учитывай дату публикации новости при выборе одежды, растительности, "
            f"продолжительности светового дня и общего сезонного вида сцены. "
            f"Если указан месяц или точная дата, одежда людей и окружающая среда должны "
            f"естественно соответствовать сезону для {city_name}, Украина. "
            f"Не выдумывай конкретную погоду только на основании даты: дата определяет сезонность, "
            f"но не означает автоматически снег, дождь, жару или пасмурную погоду."
        )

        if has_reference_image:
            system_instruction = IMAGE_REDRAW_SYSTEM_INSTRUCTION.format(city_name=city_en)
            user_content = (
                f"Заголовок новости: {title}\n"
                f"Лид новости: {lead}\n"
                f"{date_line}"
                f"Краткий контекст статьи: {article_text[:2000]}\n\n"
                f"{seasonal_instruction}\n\n"
                f"Сформируй один связный детальный промпт на английском языке для чистой перерисовки фотографии к этой новости в виде единого непрерывного кадра 16:9 (single continuous photograph, no collage, no split screen) без водяных знаков, логотипов, букв и текста."
            )
        else:
            system_instruction = IMAGE_PROMPT_SYSTEM_INSTRUCTION.format(city_name=city_en)
            user_content = (
                f"Заголовок новости: {title}\n"
                f"Лид новости: {lead}\n"
                f"{date_line}"
                f"Краткий контекст статьи: {article_text[:2000]}\n\n"
                f"{seasonal_instruction}\n\n"
                f"Сформируй один связный детальный промпт на английском языке для генерации фотореалистичной иллюстрации в виде единого кадра 16:9 (single continuous photograph, no collage, no split screen) без текста и без плакатов."
            )

        # Try prompt providers cascade
        for label, provider, model in self.prompt_providers:
            try:
                response = await provider.chat_completion(
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": user_content},
                    ],
                    model=model,
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
                f"A single horizontal 16:9 documentary editorial photograph, "
                f"natural 35mm photography, cleanly recreating the reference news scene "
                f"in {city_en}, Ukraine. Preserve the original subject, action, composition, "
                f"location type, and everyday atmosphere without making the environment "
                f"older, poorer, darker, damaged, or more dramatic than the reference. "
                f"Contemporary inhabited Ukrainian environment, realistic natural colors, "
                f"candid local photojournalism, completely textless scene, no logos, "
                f"no watermarks, no collage, no split screen, no invented emergency elements, "
                f"no dystopian or post-apocalyptic styling, photorealistic 16:9."
            )
        return (
            f"A single horizontal 16:9 documentary editorial photograph, "
            f"natural 35mm photography in {city_en}, Ukraine. "
            f"An ordinary contemporary inhabited urban environment with one or two "
            f"fictional local residents in everyday clothing naturally matching the season "
            f"going about daily life. "
            f"Realistic apartment buildings, streets, sidewalks, trees with natural seasonal foliage, "
            f"parked everyday cars, and normal city infrastructure appropriate to the story. "
            f"Neutral natural daylight, realistic colors, candid local photojournalism. "
            f"No generators, no emergency water containers, no repair crews, no rubble, "
            f"no abandoned buildings, no invented emergency, no poverty aesthetic, "
            f"no dystopian or post-apocalyptic styling. Completely textless scene, "
            f"no logos, no watermarks, no collage, no split screen, photorealistic 16:9."
        )

    async def generate_image(
        self,
        prompt: str,
        output_dir: Optional[Path | str] = None,
        model_name: str = "gemini-3.1-flash-lite-image",
        reference_image_bytes: Optional[bytes] = None,
    ) -> Optional[Path]:
        """Generate image using OpenRouter direct routing with Google key failover and save to disk."""
        if output_dir is None:
            base_dir = getattr(self.config.settings.article, "fallback_save_dir", "data/articles")
            output_dir = Path(base_dir) / "images"
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_file = output_dir / f"editorial_{timestamp}.jpg"

        # Tier 1: Direct OpenRouter Image Generation (Primary)
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
                            "OpenRouter image generation returned HTTP %s: %s; trying fallback slots",
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
                self.logger.warning(
                    "OpenRouter image generation exception: %s; trying fallback slots", exc
                )

        # Tier 2: Direct Google Gemini API keys (Fallback if OpenRouter unconfigured/failed)
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

        self.logger.warning("All image generation slots exhausted; returning None")
        return None
