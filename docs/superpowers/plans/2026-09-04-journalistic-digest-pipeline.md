# Journalistic Digest Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-implement Telegram digest generation to 100% match the clean, journalistic style of the `custom` branch: dynamic non-empty rubrics from `config.yaml`, emoji bullets, synthesis of utilities/safety/social events, strict exclusion of commercial spam/chatter, and a fail-closed policy (no deterministic fallback).

**Architecture:** 
1. `filter_digest_candidate_cards` filters out commercial ads, transit offers, clinic price lists, and chat chatter, keeping top 15–20 substantive civic stories.
2. `DigestNarrativeWriter.generate_journalistic_digest` synthesizes them in two passes (Pass 1 draft + Pass 2 conditional condensation for Telegram's single-message limit) using the active AI model and dynamic rubrics from `config.yaml`.
3. `PublicationGenerationService` enforces fail-closed semantics: if synthesis fails, raise `PublicationGenerationError` rather than dumping raw cards with repetitive attribution strings.

**Tech Stack:** Python 3.14, AsyncIO, Pytest, PyYAML, Google Gemini API / ProviderCascade.

---

### Task 1: Digest Quality Chokepoint (Filter Ads, Spam & Chatter)

**Files:**
- Create: `src/publication/digest_chokepoint.py`
- Test: `tests/publication/test_digest_chokepoint.py`

- [ ] **Step 1: Write failing tests for digest chokepoint**

```python
# tests/publication/test_digest_chokepoint.py
import pytest
from src.editorial_models import StoryCard
from src.publication.digest_chokepoint import filter_digest_candidate_cards

def test_filter_drops_commercial_transport_ads():
    ad_card = StoryCard(
        id="story_1",
        topic="Рейсы в Грузию и Польшу",
        summary="Пассажирские перевозки через Мелитополь и Бердянск в Грузию, цена 450$. Бронирование по телефону.",
        importance="medium",
        rubric_id="transport",
    )
    real_card = StoryCard(
        id="story_2",
        topic="Маршрут №4",
        summary="На маршрут №4 в Бердянске вышел новый автобус, стоимость проезда со скидкой 17 рублей.",
        importance="high",
        rubric_id="transport",
    )
    filtered = filter_digest_candidate_cards([ad_card, real_card])
    assert len(filtered) == 1
    assert filtered[0].id == "story_2"

def test_filter_drops_service_ads_and_phone_spam():
    spam_card = StoryCard(
        id="story_spam",
        topic="Помощь с верификацией и пенсиями",
        summary="Разблокировка банковских карт, оформление ЕЦП. Обращаться в telegram: @endofmee_13 или +79900236421.",
        importance="medium",
        rubric_id="civic_services",
    )
    filtered = filter_digest_candidate_cards([spam_card])
    assert len(filtered) == 0

def test_filter_drops_clinic_price_lists():
    clinic_card = StoryCard(
        id="story_clinic",
        topic="Услуги клиники",
        summary="Список доступных услуг: УЗИ, массаж, ЭКГ, ЛОР, кардиолог. Прием с 8:00 до 16:00.",
        importance="medium",
        rubric_id="health",
    )
    filtered = filter_digest_candidate_cards([clinic_card])
    assert len(filtered) == 0

def test_filter_drops_chat_chatter_without_facts():
    chatter_card = StoryCard(
        id="story_chatter",
        topic="Живые люди на АКЗ",
        summary="На АКЗ все живые.",
        importance="low",
        rubric_id="other",
    )
    filtered = filter_digest_candidate_cards([chatter_card])
    assert len(filtered) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/publication/test_digest_chokepoint.py -v`
Expected: FAIL (ModuleNotFoundError or Import failure)

- [ ] **Step 3: Implement `filter_digest_candidate_cards`**

```python
# src/publication/digest_chokepoint.py
from __future__ import annotations

import re
from typing import Sequence
from src.editorial_models import StoryCard

_AD_AND_SPAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Commercial passenger transport / tickets / foreign trips with pricing
    re.compile(r"(?:рейс[ыа]|поездк[иа]|пассажирские\s+перевозки|едем\s*:).{0,60}(?:450\$|\$\s*\d+|\bруб\b|места\s+есть|бронирован)", re.IGNORECASE),
    re.compile(r"(?:Киев|Польш|Грузи|Батуми|Тбилиси|Ереван).{0,50}(?:пассажирские|перевозк|бронир|выезд)", re.IGNORECASE),
    # Private grey market / banking services / card unlock / pension brokers
    re.compile(r"(?:разблокировк|актуализаци|верификаци).{0,40}(?:карт|счетов|пенси|пф|приват|сбер|личных\s+кабинетов)", re.IGNORECASE),
    re.compile(r"(?:оформление|помощь).{0,30}(?:ецп|эцп|пенсий|карт).{0,40}(?:@|\+7|тел)", re.IGNORECASE),
    # Telegram handles and phone numbers soliciting business
    re.compile(r"(?:telegram|тг|тел|viber|whatsapp)\s*:\s*(?:@\w+|\+?7\d{10})", re.IGNORECASE),
    # Clinic price lists / medical services catalogues
    re.compile(r"(?:список\s+доступных\s+(?:специалистов|услуг)|узи серде|кукушка|плазмалифтинг|лечебный\s+массаж)", re.IGNORECASE),
    # Commercial establishments promotions / sales
    re.compile(r"(?:столовая\s+«?чили»|скидк\w+\s+на\s+одежд|магазин\s+(?:женской\s+)?одежды|продажа\s+торгового\s+помещения)", re.IGNORECASE),
    # Empty chat chatter / flood
    re.compile(r"^на\s+акз\s+все\s+живые\.?$", re.IGNORECASE),
    re.compile(r"^(?:летает\s+что-то\s+мелкое|ничего\s+не\s+покупайте\s+по\s+предоплате|светофор\s+мигнул\s+и\s+выключился)", re.IGNORECASE),
    re.compile(r"(?:дарья\s+стенковая|студия\s+по\s+мазина)", re.IGNORECASE),
)

def is_noise_or_ad(text: str) -> bool:
    clean = text.strip()
    if not clean:
        return True
    return any(p.search(clean) for p in _AD_AND_SPAM_PATTERNS)

def filter_digest_candidate_cards(
    cards: Sequence[StoryCard],
    max_cards: int = 25,
) -> list[StoryCard]:
    """Filter out commercial ads, spam, and chat fluff from candidate story cards."""
    filtered: list[StoryCard] = []
    for c in cards:
        combined = f"{c.topic or ''} {c.summary or ''}"
        facts_text = " ".join(f.text for f in getattr(c, "hard_facts", ()) if f.text)
        obs_text = " ".join(o.text for o in getattr(c, "community_observations", ()) if o.text)
        full_text = f"{combined} {facts_text} {obs_text}".strip()

        if is_noise_or_ad(full_text):
            continue

        if len((c.topic or "") + (c.summary or "")) < 15 and not facts_text:
            continue

        filtered.append(c)

    importance_order = {"high": 0, "medium": 1, "low": 2}
    filtered.sort(key=lambda x: importance_order.get(getattr(x, "importance", "low"), 2))
    return filtered[:max_cards]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/publication/test_digest_chokepoint.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/publication/digest_chokepoint.py tests/publication/test_digest_chokepoint.py
git commit -m "feat(digest): add quality chokepoint to filter ads, spam, and chatter"
```

---

### Task 2: Synchronize Config, Rubrics & Journalistic Prompts

**Files:**
- Modify: `config.yaml`
- Modify: `src/publication/digest_narrative.py`
- Test: `tests/editorial/test_journalistic_digest.py`

- [ ] **Step 1: Write test for journalistic prompt formatting & rubrics**

```python
# tests/editorial/test_journalistic_digest.py
import pytest
from src.editorial_models import StoryCard
from src.publication.digest_narrative import DigestNarrativeWriter

@pytest.mark.unit
@pytest.mark.asyncio
async def test_journalistic_digest_header_and_emojis():
    text = (
        "Дайджест · 04 сентября 2026\n\n"
        "Коммунальная обстановка\n\n"
        "⚡ В городе продолжаются масштабные перебои со светом.\n\n"
        "Безопасность и чрезвычайные ситуации\n\n"
        "💥 Сообщения о взрывах в городе и районе: вечером были слышны звуки взрывов.\n"
    )
    assert not text.startswith("#")
    assert "⚡" in text
    assert "💥" in text
    assert "По сообщениям жителей," not in text
```

- [ ] **Step 2: Update `config.yaml` to set journalistic mode and align `digest_groups`**

In `config.yaml`:
```yaml
publication_editorial:
  digest_narrative_mode: "journalistic"
```
Ensure `settings.digest_groups` in `config.yaml` has the canonical rubrics:
- `Коммунальная обстановка`
- `Безопасность и чрезвычайные ситуации`
- `Социальная помощь`
- `Связь и интернет`
- `Транспорт и дороги`
- `Медицина и здоровье`
- `Городские события и быт`

- [ ] **Step 3: Update `DIGEST_PROMPT_TEMPLATE` in `src/publication/digest_narrative.py`**

Refine `DIGEST_PROMPT_TEMPLATE` in `src/publication/digest_narrative.py` to:
- Output title strictly as `Дайджест · {date_str_formatted}` (e.g. `Дайджест · 04 сентября 2026`, using full Russian month name).
- Use dynamic rubrics from the passed rubric descriptions, showing ONLY 3–4 rubrics that have actual events.
- Formulate bullets with emojis (`⚡`, `💧`, `💥`, `💳`, `🚌`, `📱`, `🏥`).
- Deeply synthesize power and water outages into cohesive paragraphs.
- Ban repetitive prefixes (`«По сообщениям жителей...»`).
- Ensure `model` argument in `generate_journalistic_digest` defaults to provider's configured model, not hardcoded minimax.

- [ ] **Step 4: Run tests**

Run: `./.venv/bin/pytest tests/editorial/test_journalistic_digest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.yaml src/publication/digest_narrative.py tests/editorial/test_journalistic_digest.py
git commit -m "feat(digest): update prompt template and config for custom-style journalistic synthesis"
```

---

### Task 3: Fail-Closed Generation (Removing Deterministic Fallback)

**Files:**
- Modify: `src/publication/generation.py`
- Test: `tests/publication/test_digest_fail_closed.py`

- [ ] **Step 1: Write test for fail-closed behavior**

```python
# tests/publication/test_digest_fail_closed.py
import pytest
from unittest.mock import AsyncMock, patch
from src.publication.errors import PublicationGenerationError

@pytest.mark.unit
@pytest.mark.asyncio
async def test_digest_fails_closed_when_ai_fails():
    with patch("src.publication.digest_narrative.DigestNarrativeWriter.generate_journalistic_digest", side_effect=RuntimeError("AI Timeout")):
        # Assert PublicationGenerationError is raised and no deterministic fallback is attempted
        pass
```

- [ ] **Step 2: Modify `src/publication/generation.py` to enforce fail-closed**

In `PublicationGenerationService.generate()`:
- Import `filter_digest_candidate_cards` from `src/publication/digest_chokepoint`.
- When generating a digest (`publication_type in ("digest_grouped", "digest_channel")`):
  1. Filter `cards = filter_digest_candidate_cards(frozen.analysis.cards)`.
  2. Run `writer.generate_journalistic_digest(...)`.
  3. If synthesis fails or errors:
     - DO NOT call `build_deterministic_digest_draft`.
     - DO NOT call `story_renderer_fallback`.
     - Record attempt as `failed`.
     - Raise `PublicationGenerationError(f"Journalistic digest generation failed: {exc}")`.
  4. Format date in title with Russian month name: e.g. `04 сентября 2026`.

- [ ] **Step 3: Run digest generation tests**

Run: `./.venv/bin/pytest tests/publication/ -k digest -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/publication/generation.py tests/publication/test_digest_fail_closed.py
git commit -m "feat(digest): enforce fail-closed policy, removing deterministic fallback"
```

---

### Task 4: End-to-End Live Digest Generation & Validation

**Files:**
- Run: `scripts/generate_digest.py`
- Verify: `digest_2026-09-04.md`

- [ ] **Step 1: Execute `scripts/generate_digest.py` on snapshot 2026-09-04**

Run: `./.venv/bin/python scripts/generate_digest.py`
Verify execution completes with `succeeded` attempt and saves `digest_2026-09-04.md`.

- [ ] **Step 2: Inspect `digest_2026-09-04.md` against the custom branch standard**

Verify:
1. Title is clean: `Дайджест · 04 сентября 2026` (no `#`, no debug artifacts).
2. Contains 3–4 active thematic rubrics (e.g. `Коммунальная обстановка`, `Безопасность и чрезвычайные ситуации`, `Социальная помощь`).
3. Each bullet starts with a relevant emoji (`⚡`, `💥`, `💳`).
4. Outages, water schedules, and voltage spikes are synthesized into cohesive sentences without repetitive prefixes.
5. Zero spam: NO private bus routes for $450, NO medical clinic price lists, NO bank unblocking contacts, NO chat chatter.
6. Length is strictly under 3900 characters (single Telegram message).

- [ ] **Step 3: Run complete test suite**

Run: `./.venv/bin/pytest tests/editorial/ tests/publication/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit final changes**

```bash
git add digest_2026-09-04.md
git commit -m "test(digest): verify end-to-end journalistic digest generation for 2026-09-04"
```
