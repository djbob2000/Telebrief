# Editorial Journalistic Article Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the daily editorial article generation into a cohesive, high-quality publication («Чем жил Бердянск за последние сутки») with tolerant syntax normalization, granular ref sanitization, assistance-aware filtering, synthesis-aware auditing, and a 3–5 chapter journalistic composition contract.

**Architecture:**
- **Tolerant Model & Provenance Layer** (`src/editorial_models.py`): Dataclass support for `representative_source_refs`, string element coercion with `status="attributed"`, optional `Uncertainty.basis` defaulting to `"unspecified"`, robust nested list item error isolation without logger dependencies, and pure `sanitized_against_refs()` methods.
- **Resilient Analysis Parser** (`src/editorial_analysis.py`): Granular element and card sanitization in `EditorialAnalyzer` ensuring valid cards reach the writer and raising `EditorialAnalysisError` only if 0 cards remain.
- **Assistance-Aware Input Filtering** (`src/editorial_input.py`): Preservation of mutual aid and community support messages (charging, water, heating) in `EditorialInputBuilder._looks_commercial` without mutating `source_type`.
- **Journalistic Writer & Style Contract** (`.agents/skills/news-style/SKILL.md`, `src/editorial_writer.py`): Narrative 3–5 chapter composition, lead with 2–3 themes, adaptive dominance, and collective observation synthesis.
- **Synthesis-Aware Light Fact-Checker** (`src/editorial_audit.py`): Calibrated audit prompt treating collective resident synthesis as PASS/WARN and reserving FIX for unverified facts, lost attribution, false causality, and high-risk claims.

**Tech Stack:** Python 3.10+, dataclasses, regex, pytest, asyncio.

---

## Global Constraints

1. **"Normalize syntax aggressively; infer semantics conservatively"**: Tolerate alternative schema shapes from models, but never fabricate facts, dates, names, causes, or links.
2. **Never invent or guess source refs**: Every retained ref must resolve to `PreparedBundle.records`. Unknown refs are stripped locally; an element or card is dropped only if no valid refs remain.
3. **`source_type` is immutable**: Channel/topic roles are determined exclusively by `SourceRoleResolver` from configuration and must never be altered by text heuristics.
4. **Valid cards and elements are isolated from failures**: Malformed list items (`None`, empty dicts, invalid types) are caught locally and skipped; a valid card or element is never dropped merely because a neighbouring card or sibling element is malformed.
5. **Technical placeholders remain internal**: Placeholder values like `basis="unspecified"` must never be treated by the writer as evidence or rendered as text in the final article.

---

### Task 1: Tolerant Models & Granular Provenance Sanitization (`src/editorial_models.py`, `src/editorial_analysis.py`)

**Files:**
- Modify: `src/editorial_models.py`
- Modify: `src/editorial_analysis.py`
- Test: `tests/test_editorial_models.py`
- Test: `tests/test_editorial_analysis.py`

- [ ] **Step 1.1: Write failing unit tests for tolerant model deserialization, nested item error isolation, and `representative_source_refs`**

In `tests/test_editorial_models.py`:

```python
def test_story_element_from_string_with_card_refs():
    elem = StoryElement.from_dict("Жители сообщали об отключениях", card_refs=["S000001", "S000002"])
    assert elem.text == "Жители сообщали об отключениях"
    assert elem.source_refs == ["S000001", "S000002"]
    assert elem.status == "attributed"


def test_story_element_from_string_without_card_refs_fails():
    with pytest.raises(ValueError, match="source_refs"):
        StoryElement.from_dict("Текст без рефов", card_refs=[])


def test_uncertainty_without_basis_defaults_to_unspecified():
    unc = Uncertainty.from_dict({"text": "Неясны сроки ремонта", "related_source_refs": ["S000001"]})
    assert unc.text == "Неясны сроки ремонта"
    assert unc.basis == "unspecified"
    assert unc.related_source_refs == ["S000001"]


def test_uncertainty_from_string():
    unc = Uncertainty.from_dict("Неясны сроки ремонта", card_refs=["S000001"])
    assert unc.text == "Неясны сроки ремонта"
    assert unc.basis == "unspecified"
    assert unc.related_source_refs == ["S000001"]


def test_story_card_canonical_representative_refs_from_aliases():
    data = {
        "id": "SC001",
        "topic": "Электричество",
        "summary": "Массовые отключения",
        "sources": ["S000001", "S000002", "S000001"],  # deduplication & alias
        "hard_facts": ["Света нет на АКЗ"],
        "uncertainties": ["Сроки включения неизвестны"],
    }
    card = StoryCard.from_dict(data)
    assert card.representative_source_refs == ["S000001", "S000002"]
    assert len(card.hard_facts) == 1
    assert card.hard_facts[0].source_refs == ["S000001", "S000002"]
    assert card.hard_facts[0].status == "attributed"
    assert card.all_source_refs() == {"S000001", "S000002"}


def test_story_card_keeps_valid_nested_element_when_siblings_are_malformed():
    data = {
        "id": "SC001",
        "topic": "Свет",
        "summary": "Отключения",
        "representative_source_refs": ["S000001"],
        "hard_facts": [
            None,
            {"text": ""},  # empty text
            12345,  # wrong type
            "Жители АКЗ сообщали об отключениях",  # valid string
            {"text": "Подтверждено КП", "source_refs": ["S000001"], "status": "established"},  # valid dict
        ],
        "uncertainties": [
            None,
            {"text": "Неясны сроки"},  # valid dict with default basis
        ],
    }
    card = StoryCard.from_dict(data)
    assert len(card.hard_facts) == 2
    assert card.hard_facts[0].text == "Жители АКЗ сообщали об отключениях"
    assert card.hard_facts[0].source_refs == ["S000001"]
    assert card.hard_facts[1].text == "Подтверждено КП"
    assert len(card.uncertainties) == 1
    assert card.uncertainties[0].text == "Неясны сроки"
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `uv run pytest tests/test_editorial_models.py -k "test_story_element_from_string or test_uncertainty_without_basis or test_story_card_canonical or test_story_card_keeps_valid_nested_element"`
Expected: FAIL

- [ ] **Step 1.3: Implement tolerant models in `src/editorial_models.py`**

In `src/editorial_models.py`:
- In `StoryElement`:
  - Update `from_dict(data: dict[str, Any] | str, card_refs: list[str] | None = None) -> StoryElement`
  - If `isinstance(data, str)`: create with `text=data.strip()`, `source_refs=list(card_refs or [])`, `status="attributed"`.
  - If `isinstance(data, dict)`: extract `source_refs` from `source_refs`, `sources`, `refs`, `evidence`, or fallback to `card_refs or []`. Default `status` to `"attributed"`.
- In `Uncertainty`:
  - Update `from_dict(data: dict[str, Any] | str, card_refs: list[str] | None = None) -> Uncertainty`
  - If `isinstance(data, str)`: create with `text=data.strip()`, `basis="unspecified"`, `related_source_refs=list(card_refs or [])`.
  - If `isinstance(data, dict)`: default `basis` to `"unspecified"`, `related_source_refs` to `list(card_refs or [])`.
- In `StoryCard`:
  - Add `representative_source_refs: list[str] = field(default_factory=list)` to dataclass fields.
  - In `from_dict`: extract `representative_source_refs` from `representative_source_refs`, `source_refs`, `sources`, `refs`, `evidence` (preserving order, removing duplicates).
  - Normalize nested list fields (`hard_facts`, `community_observations`, `useful_details`, `uncertainties`):
    - Handle `None` as `[]`.
    - Iterate each item in list: try `StoryElement.from_dict(item, card_refs=rep_refs)` / `Uncertainty.from_dict(item, card_refs=rep_refs)`. Catch `(AttributeError, TypeError, ValueError)` locally and skip only that malformed item (keeping model layer pure without logger dependencies).
  - In `all_source_refs()`: include `self.representative_source_refs` in the returned set.

- [ ] **Step 1.4: Run tests to verify Step 1.3 passes**

Run: `uv run pytest tests/test_editorial_models.py`
Expected: PASS

- [ ] **Step 1.5: Write failing unit tests for granular sanitization in `src/editorial_models.py` and `src/editorial_analysis.py`**

In `tests/test_editorial_models.py`:

```python
def test_story_card_sanitized_against_refs_removes_bad_ref_locally():
    card = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Отключения",
        representative_source_refs=["S000001", "S999999"],
        hard_facts=[
            StoryElement(text="Факт 1", source_refs=["S000001", "S999999"]),
            StoryElement(text="Факт 2 (только битый ref)", source_refs=["S999999"]),
        ],
    )
    sanitized = card.sanitized_against_refs({"S000001"})
    assert sanitized is not None
    assert sanitized.representative_source_refs == ["S000001"]
    assert len(sanitized.hard_facts) == 1
    assert sanitized.hard_facts[0].text == "Факт 1"
    assert sanitized.hard_facts[0].source_refs == ["S000001"]


def test_story_card_sanitized_against_refs_returns_none_if_no_valid_refs():
    card = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Отключения",
        representative_source_refs=["S999999"],
        hard_facts=[StoryElement(text="Факт", source_refs=["S999999"])],
    )
    assert card.sanitized_against_refs({"S000001"}) is None


def test_editorial_analysis_sanitized_against_refs():
    card1 = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Отключения",
        representative_source_refs=["S000001"],
    )
    card2 = StoryCard(
        id="SC002",
        topic="Газ",
        importance="low",
        summary="Газ",
        representative_source_refs=["S999999"],
    )
    analysis = EditorialAnalysis(
        cards=[card1, card2],
        labels={"S000001": {"flag": "ok"}, "S999999": {"flag": "bad"}},
    )
    sanitized = analysis.sanitized_against_refs({"S000001"})
    assert len(sanitized.cards) == 1
    assert sanitized.cards[0].id == "SC001"
    assert "S000001" in sanitized.labels
    assert "S999999" not in sanitized.labels
```

In `tests/test_editorial_analysis.py`:

```python
@pytest.mark.asyncio
async def test_analyzer_analyze_sanitizes_refs_and_succeeds_with_partial_bad_refs(mock_logger):
    payload = json.dumps({
        "cards": [
            {
                "id": "SC001",
                "topic": "Вода",
                "importance": "high",
                "summary": "Воду отключили.",
                "sources": ["S000001", "S999999"],
                "hard_facts": [
                    {"text": "Предприятие сообщило об отключении.", "source_refs": ["S000001", "S999999"]}
                ],
            },
            {
                "id": "SC002",
                "topic": "Фантом",
                "importance": "low",
                "summary": "Фантомная тема.",
                "sources": ["S999999"],
                "hard_facts": [{"text": "Фантомный факт", "source_refs": ["S999999"]}],
            },
        ]
    })
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=payload)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    analysis = await analyzer.analyze(_bundle())
    assert len(analysis.cards) == 1
    assert analysis.cards[0].id == "SC001"
    assert analysis.cards[0].representative_source_refs == ["S000001"]
    assert analysis.cards[0].hard_facts[0].source_refs == ["S000001"]
```

- [ ] **Step 1.6: Run test to verify it fails**

Run: `uv run pytest tests/test_editorial_models.py tests/test_editorial_analysis.py -k "test_story_card_sanitized or test_editorial_analysis_sanitized or test_analyzer_analyze_sanitizes"`
Expected: FAIL

- [ ] **Step 1.7: Implement `sanitized_against_refs` in `src/editorial_models.py` and integrate in `src/editorial_analysis.py`**

In `src/editorial_models.py`:
- Implement `StoryCard.sanitized_against_refs(self, available_refs: set[str]) -> StoryCard | None`:
  - Filter `representative_source_refs` to available refs.
  - Filter elements in `hard_facts`, `community_observations`, `useful_details` by keeping only elements with at least one valid ref in `available_refs`.
  - Filter `uncertainties` by sanitizing `related_source_refs`.
  - Sanitize `editorial_angle.basis_refs`.
  - Return `None` if no valid elements and no valid `summary + representative_source_refs` remain.
  - Return new sanitized `StoryCard`.
- Implement `EditorialAnalysis.sanitized_against_refs(self, available_refs: set[str]) -> EditorialAnalysis`:
  - Map cards through `card.sanitized_against_refs(available_refs)` and keep non-None.
  - Filter `labels` and `excluded_refs` to `available_refs`.
  - Return new sanitized `EditorialAnalysis`.

In `src/editorial_analysis.py`:
- Update `EditorialAnalyzer._normalize_card_payload`: extract card-level refs into `representative_source_refs` and normalize list elements.
- Update `EditorialAnalyzer.analyze`:

```python
analysis = await self._call_analysis(bundle, compact=compact)
sanitized = analysis.sanitized_against_refs(set(bundle.records))
if not sanitized.cards:
    raise self._failure("invalid_source_ref", "no valid Story Cards remain after reference sanitization")
return sanitized
```

- Update `EditorialAnalyzer.analyze_batched` similarly.

- [ ] **Step 1.8: Run all tests in `test_editorial_models.py` and `test_editorial_analysis.py`**

Run: `uv run pytest tests/test_editorial_models.py tests/test_editorial_analysis.py`
Expected: ALL PASS

- [ ] **Step 1.9: Commit Task 1**

```bash
git add src/editorial_models.py src/editorial_analysis.py tests/test_editorial_models.py tests/test_editorial_analysis.py
git commit -m "feat: add tolerant model parsing and granular provenance sanitization"
```

---

### Task 2: Assistance-Aware Commercial Filtering (`src/editorial_input.py`)

**Files:**
- Modify: `src/editorial_input.py`
- Test: `tests/test_editorial_input.py`

- [ ] **Step 2.1: Write failing unit tests for assistance-aware filtering (positive and negative pairs)**

In `tests/test_editorial_input.py`:

```python
def test_looks_commercial_preserves_mutual_aid_pairs():
    # Mutual aid / assistance must be KEPT (return False from _looks_commercial)
    assert not EditorialInputBuilder._looks_commercial(
        "В кафе сегодня можно бесплатно зарядить телефон, есть генератор", source_type="community"
    )
    assert not EditorialInputBuilder._looks_commercial(
        "Подвоз воды жителям АКЗ бесплатно во двор", source_type="community"
    )
    assert not EditorialInputBuilder._looks_commercial(
        "В ДК открыт пункт обогрева, можно набрать воды и зарядить гаджеты", source_type="community"
    )


def test_looks_commercial_still_filters_commercial_pairs():
    # Commercial advertisements must be DROPPED (return True from _looks_commercial)
    assert EditorialInputBuilder._looks_commercial(
        "Бесплатная консультация, звоните +79901234567, запись по телефону", source_type="community"
    )
    assert EditorialInputBuilder._looks_commercial(
        "Доставка воды, выгодные цены, звоните +79901234567", source_type="community"
    )
    assert EditorialInputBuilder._looks_commercial(
        "Обмен валют по лучшему курсу, обналичивание карт в центре", source_type="community"
    )


def test_editorial_input_builder_never_mutates_source_type():
    from datetime import datetime, timezone
    from src.collector import Message
    from src.config_loader import ChannelConfig, SourceRoleResolver

    resolver = SourceRoleResolver([ChannelConfig(id="@test_channel", name="test_channel", source_type="community")])
    builder = EditorialInputBuilder(resolver)
    msg = Message(
        text="В кафе можно бесплатно зарядить телефон",
        channel_name="test_channel",
        timestamp=datetime.now(timezone.utc),
        sender="admin",
        message_id=1,
    )
    bundle = builder.build({"test_channel": [msg]})
    assert "S000001" in bundle.records
    assert bundle.records["S000001"].source_type == "community"
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `uv run pytest tests/test_editorial_input.py -k "test_looks_commercial_preserves_mutual_aid or test_editorial_input_builder_never_mutates"`
Expected: FAIL

- [ ] **Step 2.3: Implement assistance-aware filtering in `src/editorial_input.py`**

In `src/editorial_input.py`:
- Add `_MUTUAL_AID_MARKERS = re.compile(r"(?:бесплатно\s+(?:зарядить|набрать|разда)|подвоз\s+(?:питьев|техническ)?воды|раздач[аеи]\s+воды|пункт\s+обогрев|помощь\s+сосед|поделит[ьс]ся\s+генератор)", re.IGNORECASE)`
- In `_looks_commercial(text: str, source_type: str = "mixed") -> bool`:
  - Preserve existing explicit commercial/financial predicates.
  - If explicit commercial spam matches (`_EXPLICIT_COMMERCIAL`), return `True`.
  - Otherwise allow `_MUTUAL_AID_MARKERS` to exempt genuine assistance before continuing standard commercial heuristics. Do not introduce a new financial helper unless required by a failing test.

- [ ] **Step 2.4: Run all tests in `test_editorial_input.py`**

Run: `uv run pytest tests/test_editorial_input.py`
Expected: ALL PASS

- [ ] **Step 2.5: Commit Task 2**

```bash
git add src/editorial_input.py tests/test_editorial_input.py
git commit -m "feat: preserve community mutual aid and assistance messages in editorial input"
```

---

### Task 3: Journalistic Writer Contract & News Style Update (`.agents/skills/news-style/SKILL.md`, `src/editorial_writer.py`, `src/article_generator.py`)

**Files:**
- Modify: `.agents/skills/news-style/SKILL.md`
- Modify: `src/editorial_writer.py`
- Modify: `src/article_generator.py`
- Test: `tests/test_editorial_writer.py`

- [ ] **Step 3.0: Mandatory skill editing protocol**

Before modifying `.agents/skills/news-style/SKILL.md`, invoke `skill-creator` and follow its instructions. Preserve the approved editorial contract; do not redesign it.

- [ ] **Step 3.1: Write failing tests for news style skill content and writer prompt composition contract**

In `tests/test_editorial_writer.py`:

```python
def test_editorial_writer_prompt_contains_thematic_chapters_and_synthesis_rules():
    writer = EditorialWriter(
        provider=None,
        model="test-model",
        skill_instructions="Newsroom style guidelines",
        logger=logging.getLogger("test"),
    )
    analysis = EditorialAnalysis(
        cards=[
            StoryCard(
                id="SC001",
                topic="Свет",
                importance="high",
                summary="Отключения",
                representative_source_refs=["S000001"],
            )
        ]
    )
    bundle = PreparedBundle(records={}, prompt_text="", total_messages=1, candidate_count=1)
    system, _ = writer.build_prompt(analysis, bundle)
    assert "3–5" in system or "3-5" in system
    assert "lead" in system.lower()
    assert "Story Cards are reporting notes" in system


def test_news_style_skill_file_contains_approved_composition_contract():
    from src.article_generator import _load_skill_instructions

    content = _load_skill_instructions(".agents/skills/news-style/SKILL.md")
    assert "3–5" in content or "3-5" in content
    assert "жители" in content.lower() or "resident" in content.lower()
    assert "causality" in content.lower() or "причинн" in content.lower()
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `uv run pytest tests/test_editorial_writer.py -k "test_editorial_writer_prompt_contains or test_news_style_skill_file"`
Expected: FAIL

- [ ] **Step 3.3: Update news style skill instructions and writer prompts**

- In `.agents/skills/news-style/SKILL.md`:
  - Specify composition contract:
    - Headline: captures the key supported event, condition, or theme of the day. Collective mood/emotion appears only when directly supported by source material.
    - Lead: 1–2 sentences highlighting 2–3 prominent themes (no category inventory).
    - 3–5 thematic story chapters with descriptive journalistic headings (`## [Тематический заголовок]`).
    - Synthesizing resident comments across districts into narrative paragraphs with visible attribution (`по сообщениям жителей`, `в районных чатах отмечали`).
    - Related same-day observations (outages, residents looking for charging, venues offering free charging) covered together without unsupported causal jumps.
    - Adaptive dominance for a major event without a rigid percentage.
    - Timeline progression inside chapters when helpful.
- In `src/editorial_writer.py`:
  - Update `build_prompt` system text to embed the composition rules.
- In `src/article_generator.py`:
  - Update `_compose_system_prompt` compatibility helper.

- [ ] **Step 3.4: Run all tests in `test_editorial_writer.py`**

Run: `uv run pytest tests/test_editorial_writer.py`
Expected: ALL PASS

- [ ] **Step 3.5: Commit Task 3**

```bash
git add .agents/skills/news-style/SKILL.md src/editorial_writer.py src/article_generator.py tests/test_editorial_writer.py
git commit -m "feat: update editorial writer prompt and news style composition contract"
```

---

### Task 4: Synthesis-Aware Audit & Pipeline Regression Test (`src/editorial_audit.py`, `tests/test_editorial_audit.py`, `tests/test_article_generator.py`)

**Files:**
- Modify: `src/editorial_audit.py`
- Test: `tests/test_editorial_audit.py`
- Test: `tests/test_article_generator.py`

- [ ] **Step 4.1: Write failing unit test for fact-checker prompt instructions**

In `tests/test_editorial_audit.py`:

```python
def test_light_fact_checker_prompt_distinguishes_synthesis_from_unverified_facts():
    checker = LightFactChecker(
        provider=None,
        model="test-model",
        logger=logging.getLogger("test"),
    )
    prompt = checker._build_system_prompt()
    assert "synthesis" in prompt.lower()
    assert "FIX" in prompt
    assert "WARN" in prompt
    assert "verifiable" in prompt.lower() or "unverified" in prompt.lower()
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `uv run pytest tests/test_editorial_audit.py -k "test_light_fact_checker_prompt_distinguishes"`
Expected: FAIL

- [ ] **Step 4.3: Implement `_build_system_prompt()` and update `LightFactChecker` in `src/editorial_audit.py`**

In `src/editorial_audit.py`:
- Add `_build_system_prompt(self) -> str` method returning:

```python
def _build_system_prompt(self) -> str:
    return (
        "You are a light newsroom fact checker. Inspect the whole draft against the Story "
        "Cards and original source records. Return JSON only: status PASS, WARN or FIX, "
        "systemic_problem boolean, and issues. Find only new concrete independently "
        "verifiable facts without support: numbers, prices, dates, names, official actions, "
        "causes, mechanisms, damage, sales, medical/legal/military claims, casualties and "
        "precise scale. Legitimate collective synthesis of resident observations and "
        "discussions is not a FIX merely because no single message literally contains the "
        "whole synthesized sentence. Use PASS when well supported and WARN for soft "
        "overstatement or debatable framing; reserve FIX for unsupported verifiable facts, "
        "lost attribution, false causality or high-risk escalation. Emotional or mood assertions "
        "require direct source evidence. Attribution and source_refs are inspection aids, "
        "not proof by themselves. WARN is non-blocking; use FIX only when a local fragment "
        "must be changed."
    )
```

- Use `self._build_system_prompt()` in `LightFactChecker.check`.

- [ ] **Step 4.4: Write failing end-to-end pipeline regression test reproducing the dry-run failure payload**

In `tests/test_article_generator.py`:

```python
@pytest.mark.asyncio
async def test_regression_pipeline_recovers_from_string_elements_and_bad_refs(mock_config, logger, mocker):
    analysis_json = json.dumps({
        "cards": [
            {
                "id": "SC001",
                "topic": "Электроснабжение",
                "summary": "Отключения света на АКЗ и в Лисках",
                "sources": ["S000001", "S000002"],
                "hard_facts": [
                    "На АКЗ отключилось электричество",
                    "В Лисках также нет света",
                ],
                "uncertainties": [
                    {"text": "Сроки подачи не сообщаются"}
                ],
            },
            {
                "id": "SC002",
                "topic": "Связь",
                "summary": "Перебои у операторов",
                "sources": ["S000001", "S999999"],  # Contains 1 bad ref
                "community_observations": [
                    "Жители жалуются на мобильную связь"
                ],
            },
        ]
    })

    writer_draft = ArticleDraft(
        headline="Как Бердянск прожил сутки с перебоями света и связи",
        lead="Главными темами дня в городе стали перебои с электричеством и мобильной связью.",
        sections=[
            ArticleSection(
                heading="Перебои со светом и зарядка гаджетов: что происходило в районах",
                paragraphs=["В течение дня жители АКЗ и Лисок сообщали об отключениях электричества."],
            ),
            ArticleSection(
                heading="Ситуация с мобильной связью",
                paragraphs=["В городских чатах жители также отмечали перебои со связью."],
            ),
        ],
    )

    generator = ArticleGenerator(mock_config, logger)
    generator.provider.chat_completion = AsyncMock(side_effect=[
        analysis_json,  # Analyzer call
        json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),  # Fact-check call
    ])

    analyzer_spy = mocker.spy(generator.analyzer, "analyze")
    generator.fallback_builder.build = mocker.MagicMock(wraps=generator.fallback_builder.build)
    generator.writer.write = AsyncMock(return_value=writer_draft)

    messages = {
        "channel_1": [
            Message(text="На АКЗ нет света", channel_name="channel_1", timestamp=datetime.now(timezone.utc), sender="user1", message_id=1),
            Message(text="В Лисках тоже выключили", channel_name="channel_1", timestamp=datetime.now(timezone.utc), sender="user2", message_id=2),
        ]
    }

    title, lead, body = await generator.generate_article(messages)

    assert title == "Как Бердянск прожил сутки с перебоями света и связи"
    assert "Перебои со светом" in body
    assert generator.fallback_builder.build.call_count == 0
    assert generator.writer.write.call_count == 1
    assert analyzer_spy.call_count == 1
    assert analyzer_spy.call_args.kwargs.get("compact", False) is False

    # Verify S999999 was sanitized out and never passed to writer bundle
    call_args = generator.writer.write.call_args
    passed_analysis, passed_bundle = call_args[0]
    assert "S999999" not in passed_analysis.all_source_refs()
    assert "S999999" not in passed_bundle.records
```

- [ ] **Step 4.5: Run regression test to verify it passes**

Run: `uv run pytest tests/test_article_generator.py -k "test_regression_pipeline_recovers"`
Expected: PASS

- [ ] **Step 4.6: Run all tests in `test_editorial_audit.py` and `test_article_generator.py`**

Run: `uv run pytest tests/test_editorial_audit.py tests/test_article_generator.py`
Expected: ALL PASS

- [ ] **Step 4.7: Commit Task 4**

```bash
git add src/editorial_audit.py tests/test_editorial_audit.py tests/test_article_generator.py
git commit -m "test: add pipeline regression test and calibrate fact checker for collective synthesis"
```

---

### Task 5: Full Suite Verification & Final Check

**Files:**
- Repository-wide verification

- [ ] **Step 5.1: Run full test suite without coverage**

Run: `uv run pytest -q --no-cov`
Expected: ALL PASS

- [ ] **Step 5.2: Run static analysis and linting**

Run: `uv run flake8 src tests`
Expected: No errors

Run: `uv run mypy src`
Expected: Success: no issues found

Run: `git diff --check`
Expected: Clean (no whitespace / conflict errors)

- [ ] **Step 5.3: Run live diagnostic smoke check**

Run: `uv run python main.py --article --dry-run`
Expected: If the configured AI provider is available and returns at least one sanitizable Story Card, the run must reach writer/audit and must not enter deterministic fallback because of string StoryElements, missing Uncertainty.basis, or partially invalid refs. External provider outage/quota/timeout is not an implementation failure when the log identifies the branch and fallback remains compact.
