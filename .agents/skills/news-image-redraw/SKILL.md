---
name: news-image-redraw
description: Use when generating, conceptualizing, or crafting editorial visuals, photojournalistic scenes, and prompts for Russian/Ukrainian local news articles using Gemini / Imagen / AI image generation models from news text.
---

# News Editorial Image Generation

## Overview

Create original, high-CTR editorial visuals for local news articles based on news text (headline, lead, and key facts). The goal is to generate relatable, authentic-looking images that instantly grab attention in a Telegram feed without misleading readers or using cheap abstract graphics.

Default prompt language for image generators: **English** (for maximum rendering fidelity in Imagen / Gemini / Midjourney), while article captions and news context remain in the newsroom language (Russian/Ukrainian).

---

## Visual Style Hierarchy

### 1. Primary Style (90% of cases): Realistic Editorial Photojournalism
**When to use:** Accidents, utility outages (water/power), city life, street scenes, weather, municipal repairs, transport, local events, hospital/school topics, community reality.

* **Visual goal:** Looks like an authentic, high-quality documentary photograph taken by a local news reporter.
* **Environment:** Authentic local post-Soviet / southern seaside urban landscape (panel apartment buildings, asphalt courtyards, typical small shops, bus stops).
* **People:** Natural, fictional everyday residents in realistic casual attire matching the current season and weather. Genuine, unexaggerated facial expressions and natural poses.
* **Lighting & Camera:** Natural daylight or atmospheric dusk/night, 35mm or 50mm documentary lens, realistic depth of field, authentic textures, gritty realism without artificial studio gloss or stock-photo fake smiles.

### 2. Secondary Style (10% of cases): Editorial Cartoon / Expressive Sketch
**When to use:** Bureaucratic absurdity, convoluted laws, strange rumors, irony, satirical city situations, or topics too abstract/humorous to depict as a real photo.

* **Visual goal:** Witty, colorful newspaper editorial caricature or rich graphic-novel style sketch.
* **Characters:** Expressive, fictional characters capturing the mood of the situation (e.g., a stressed official juggling unplugged cords and leaking pipes).
* **Tone:** Sharp, thoughtful editorial humor without clownish slapstick.

*(Avoid abstract geometry, minimalistic vector icons, or corporate flat art — they lack human connection and severely reduce Telegram CTR.)*

---

## Core Generation Rules & Constraints

1. **Strictly Single Continuous Frame (`single continuous photograph, single frame`):**
   - **Crucial Anti-Collage Rule:** The AI must generate EXACTLY ONE unified continuous photograph.
   - **Explicit Negative Anchors:** `NO collage, NO split screen, NO diptych, NO multiple frames, NO photo strip, NO film contact sheet, NO before-and-after comparison, NO border frames, NO dividing lines or stripes`.
2. **Strictly NO Text on Image (`NO text, NO letters, NO words, NO signs`):** AI text rendering often produces unreadable gibberish or unintended English signage (e.g. "GROCERY", "STORE"). Explicitly prohibit English words, signage, and text on buildings, t-shirts, storefronts, and documents.
3. **Regional Topography & Architectural Authenticity:**
   - **Default Bias Risk:** Image generators by default render American suburban streets, motel-like architecture, palm trees, and US-style road markings. This destroys credibility for local residents.
   - **Mandatory Anchors for Street/Outdoor Scenes:** Explicitly specify *Eastern European / Ukrainian post-Soviet southern coastal town environment*, *typical 5-story or 9-story Soviet-era brick/panel residential apartment blocks (khrushchevka/panel houses)*, *authentic asphalt courtyards with typical local trees (poplars, acacias, chestnuts)*, *metal entrance canopies (козырьки подъездов)*, *gas facade pipes*.
   - **Explicit Negative Anchors:** `NO American architecture, NO Western suburban streets, NO palm trees, NO English storefronts`.
4. **Indoor / Medium Shots Advantage:**
   - Domestic interiors (kitchens, apartment hallways, desks, balconies) and close-up human action shots (hands with powerbanks, water bottles, faucets) are naturally authentic and minimize the risk of architectural dissonance.
5. **Season and Context Anchors:**
   - Always align visual details with the actual date/season (e.g., summer: t-shirts, shorts, green foliage; winter: heavy jackets, bare branches).
   - Match local conditions (if power outage: dark building facades with unlit windows, running portable generators on asphalt, candles/flashlights; if water outage: 5-liter plastic water jugs with blue handles).
6. **No Fabricated Disasters / Fake Evidence:**
   - Never add flames, blood, weapons, explosions, or dramatic ruins unless explicitly established in the verified news text.
   - Do not attempt to forge real private individuals or specific living citizens. All depicted persons must be generic, fictional archetypes.
7. **No Stock Photo Clichés:**
   - Avoid glowing neon lights, futuristic holograms, or overly polished commercial stock models.
8. **Aspect Ratio:**
   - Default to **16:9** (or **4:3**) for optimal display in Telegram photo posts.

---

## Prompt Recipe

Every prompt for the image generation model (Imagen 3 / Gemini Image) should follow this structured English formula in exact order:

```text
[STYLE & OPTICS]: Realistic editorial photojournalism, authentic documentary photography, 35mm lens, single unified frame, candid natural shot, natural lighting, gritty authentic textures, no studio gloss.
[PRIMARY SUBJECT & FOREGROUND]: [Fictional everyday people performing specific actions related to the story, natural clothing matching season].
[REGIONAL SETTING & CITY CONTEXT]: [Authentic scene in Berdyansk, Ukraine: e.g. Soviet-era 5-story brick/panel apartment blocks (khrushchevka), weathered balconies, yellow gas pipes on facades, asphalt courtyards with poplars/acacias].
[ATMOSPHERIC PROPS & LIGHTING]: [Concrete objects from news: portable generators on asphalt, 5-liter plastic water jugs with blue handles, warm LED flashlights, powerbanks, low-light dusk ambience].
[STRICT NEGATIVE CONSTRAINTS]: Unmarked blank facades, plain unbranded clothing, completely textless environment, NO text, NO letters, NO words on signs, NO English shopfronts, NO American suburban architecture, NO palm trees, NO watermarks, NO logos, NO artificial stock-photo gloss, NO collage, NO split screen, NO diptych, NO multiple frames, NO film strip. Single continuous photograph, photorealistic 16:9 aspect ratio.
```

---

## Workflow

1. **Extract News Context:** Read the article's Title, Lead, and key factual details (season, setting, problem).
2. **Select Style:** Choose between **Realistic Photojournalism** (default) or **Editorial Cartoon** (bureaucracy/satire).
3. **Draft the Visual Concept:** Identify 2–3 concrete, relatable visual objects/actions representing the core story.
4. **Generate the English Prompt:** Build the prompt following the Recipe with all negative constraints.
5. **Pass to Image Model:** Send the prompt to Google Imagen 3 / Gemini Image.
6. **Package for Telegram:** Output the generated image alongside the Telegram caption (Title + Lead) and the Inline button linking to the full story.
