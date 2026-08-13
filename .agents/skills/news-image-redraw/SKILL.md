---
name: news-image-redraw
description: Use when redrawing, reimagining, or replacing original images and photos from Russian/Ukrainian local news articles with new editorial visuals using ChatGPT Images 2.0, GPT Image, or an image-generation/editing workflow.
---

# News Image Redraw

## Overview

Create original editorial images for local news articles by using the source photo or article image as factual context, not as something to copy. Preserve the news meaning, setting, and practical details while changing composition, framing, lighting, and visual treatment enough that the output is a new newsroom asset.

Default to Russian prompts and captions unless the user asks for another language.

## Workflow

1. Read the article context and inspect the source image.
2. Extract the visual brief: story type, location, main subject, time of day, visible objects, mood, and any details that must not change.
3. Decide the redraw mode:
   - **Editorial photo-like** for accidents, utilities, city scenes, police, courts, hospitals, schools, events.
   - **Documentary illustration** when the source photo is low quality, legally sensitive, or includes identifiable private people.
   - **Clean explainer visual** for tariffs, schedules, routes, elections, municipal services, public notices.
   - **Neutral symbolic image** when the article has no safe or verifiable scene to visualize.
4. Generate or edit with ChatGPT Images 2.0 using the source image only as a reference when available and appropriate.
5. Review the result for factual drift, copied composition, unwanted text, face/identity issues, artifacts, and local-news usefulness.
6. Provide the final image plus a short caption, alt text, and image-generation prompt when useful.

## Redraw Rules

- Do not recreate the source image 1:1. Change crop, camera angle, object placement, background details, lighting, or style.
- Preserve factual anchors from the article: type of place, event, season, weather, visible infrastructure, vehicle type, uniform type, public building category, or municipal object.
- Avoid inventing new evidence. Do not add flames, blood, weapons, police tape, victims, official seals, military symbols, or dramatic damage unless the article/source explicitly supports them.
- Do not preserve the identity of private people, victims, minors, suspects, patients, witnesses, or social-media users. Use generic non-identifiable people or silhouettes when people are needed.
- Public officials, public figures, landmarks, and official locations may be recognizable only when that recognition is necessary and accurate.
- Remove or avoid watermarks, UI overlays, social media frames, license plates, phone numbers, street addresses on private homes, and readable personal data.
- Avoid fake news graphics: no fabricated logos, outlet branding, source labels, dates, document stamps, or screenshots.
- Prefer natural editorial realism over glossy advertising style.

## Prompt Recipe

Build the image prompt in this order:

1. Task: `Перерисуй исходное фото как новое оригинальное редакционное изображение для локальной новости.`
2. Scene: who/what/where is shown, based on the article.
3. Required facts: details that must remain true.
4. Transformation: how to differ from the source photo.
5. Style: editorial photo, documentary illustration, explainer visual, or symbolic image.
6. Safety/privacy: make people non-identifiable, remove personal data, avoid watermarks.
7. Output constraints: aspect ratio, no text unless requested, realistic lighting, clean composition.

Use this base prompt:

```text
Перерисуй исходное изображение как новое оригинальное редакционное изображение для локальной новости.
Сохрани смысл сцены: [кратко описать событие/объект/место].
Обязательные факты: [список деталей из статьи, которые нельзя менять].
Сделай другую композицию, ракурс и кадрирование; не копируй исходное фото 1:1.
Стиль: [редакционное реалистичное фото / документальная иллюстрация / чистая объясняющая визуализация].
Люди, если есть, должны быть неидентифицируемыми. Убери водяные знаки, интерфейсные элементы, личные данные и случайный текст.
Без логотипов СМИ и без надписей, если они не указаны отдельно. Формат: [16:9 / 4:3 / 1:1 / 3:2].
```

For ChatGPT Images 2.0 image editing, attach the source image and say explicitly that it is a reference for news context, not an exact-copy target.

## Story-Type Guidance

- **Accident or emergency**: show aftermath, location, responders, vehicles, or blocked road without gore or sensationalism.
- **Police or court**: use neutral documentary visuals: courthouse exterior, police car, evidence-neutral city scene, documents without readable private data.
- **Utilities and repairs**: show pipes, roadworks, workers, equipment, neighborhood streets, apartment blocks, meters, notices.
- **City council or tariffs**: use clean explainer visuals, documents, council hall, municipal building, residents at service windows.
- **Schools, culture, sports**: keep warmer community tone, but avoid identifiable minors unless the user explicitly has permission and asks for recognizability.
- **Social appeal or complaint**: visualize the practical problem and affected place; avoid humiliating or exposing private people.
- **War, occupation, violence, or tragedy**: keep restrained documentary or symbolic treatment; do not intensify harm beyond verified facts.

## Output Package

When the user asks for a ready news asset, return:

- Generated/redrawn image.
- Caption in local-news style.
- Alt text describing the image factually.
- Prompt used or a reusable prompt variant.
- Any caveats if the source photo contained private people, uncertain facts, or risky details.

## Quality Check

Before finishing, verify:

- The image tells the same news story without copying the source photo's exact composition.
- The output does not add unsupported facts or sensational details.
- Private people are not identifiable unless explicitly permitted.
- No watermarks, UI frames, personal data, unreadable fake documents, or accidental text remain.
- The visual fits a regional newsroom, not an ad, poster, meme, or generic stock image.
