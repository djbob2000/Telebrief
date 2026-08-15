# Editorial Journalistic Article Pipeline Specification

## Goal & Vision

Transform the daily long-form article generation (`--article` mode in Telebrief) from a mechanical categorization into a cohesive, high-quality, professional daily publication: **«Чем жил Бердянск за последние сутки»**.

The output must read like an article written by a local newsroom journalist who monitored the city throughout the day, reviewed official announcements and news reports, analyzed resident discussions and neighborhood comments, identified the core storylines, synthesized communal experiences (power cuts, connectivity, mutual aid, water supply, transport), and produced an engaging 3–5 chapter narrative.

---

## Core Principles

1. **"Normalize syntax aggressively; infer semantics conservatively"**:
   - The pipeline must robustly deserialize and normalize varied LLM output shapes (e.g., strings inside element lists, missing optional fields like `Uncertainty.basis`, alternative key names).
   - The pipeline must **never** invent substantive facts, causes, emotions, or source linkages.
   - Validation must be granular: drop an invalid element without dropping the card; drop an invalid card without failing the whole 15k-character analysis. Fallback to deterministic template is used **only** if zero valid cards remain after normalization.

2. **Resident Comments as Full Editorial Material**:
   - Dozens of localized comments (e.g., *"no power in AKZ"*, *"did Koloniya get power back?"*, *"where to charge phones?"*) are legitimate reporting material for journalistic collective synthesis (e.g., *"Перебои с электричеством оставались одной из главных тем городских обсуждений..."*).
   - Collective synthesis of observations does not require word-for-word single-comment proof.
   - Emotional claims (*"горожане были возмущены/напуганы"*) still require explicit direct evidence in the sources.

3. **Preserving Source Role Integrity**:
   - `source_type` is determined solely by configuration via `SourceRoleResolver` and is never overwritten by content heuristics.
   - Useful public assistance and mutual aid (free device charging, water distribution, neighborhood help) are preserved in the bundle by preventing false-positive commercial spam filtering.

4. **Composition Contract**:
   - **Headline**: Captures the key mood/event of the day.
   - **Lead**: 1–2 sentences highlighting 2–3 prominent themes of the day (no exhaustive laundry list of all categories).
   - **Body**: 3–5 thematic story chapters with engaging journalistic headings (e.g., `## Перебои со светом и поиск зарядки: что обсуждали жители районов`).
   - **Card-to-Story Flexibility**: Story Cards are reporting notes. The writer may combine related cards into single narrative blocks.
   - **Adaptive Dominance**: If one event or crisis objectively dominates the day, it receives focal treatment (expanded story), followed by concise coverage of other city topics.
   - **Internal Chronology**: Chronological sequencing is used inside individual storylines where it clarifies event progression, not as a rigid global timeline.

---

## Component Architecture & Detailed Design

```
+-----------------------------------------------------------------------------------+
| 1. Source Bundle Preparation (editorial_input.py)                                 |
|    - SourceRoleResolver sets source_type (immutable)                              |
|    - Noise & Commercial Filter updated: mutual aid / charging / water bypass spam |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
| 2. Story Card Extraction & Tolerant Normalization (editorial_models.py / analysis)|
|    - Tolerant schema deserialization (strings -> StoryElement, missing basis -> "")|
|    - Granular provenance checking: invalid element dropped, card kept if valid    |
|    - Strict ref verification: every S###### must exist in PreparedBundle          |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
| 3. Journalistic Article Writer (news-style/SKILL.md & editorial_writer.py)        |
|    - 3-5 thematic chapters with engaging narrative titles                         |
|    - Lead highlights 2-3 prominent topics                                         |
|    - Journalistic synthesis of resident comments + official reports               |
|    - Adaptive deep dive when one theme dominates the day                          |
+-----------------------------------------------------------------------------------+
                                      |
                                      v
+-----------------------------------------------------------------------------------+
| 4. Synthesis-Aware Light Fact-Checker (editorial_audit.py)                        |
|    - Distinguishes legitimate collective synthesis from unverified factual claims |
|    - FIX applied to: new unverified facts, lost attribution, dangerous high-risk  |
|    - Emotion/sentiment requires explicit source support                           |
+-----------------------------------------------------------------------------------+
```

---

### Component 1: Tolerant Normalization & Data Models (`src/editorial_models.py`, `src/editorial_analysis.py`)

#### 1.1 `StoryElement` Deserialization

- `from_dict(data, card_refs=None)`:
  - If `data` is a `str`: transforms to `StoryElement(text=data.strip(), source_refs=card_refs or [], status="attributed")`. If `card_refs` is empty, this element lacks provenance and is dropped.
  - If `data` is a `dict`:
    - `text`: non-empty string.
    - `source_refs`: extracted from `source_refs`, `sources`, `refs`, or fallback to `card_refs`.
    - `status`: validated against `{"established", "attributed", "disputed"}`, defaults to `"attributed"`.
    - `attribution`: string, defaults to `""`.
    - `areas`: list of strings, defaults to `[]`.
  - Rejection: if `text` is empty or `source_refs` is empty after fallback, raise `ValueError` (caught locally to drop only this element).

#### 1.2 `Uncertainty` Deserialization

- `from_dict(data, card_refs=None)`:
  - If `data` is a `str`: transforms to `Uncertainty(text=data.strip(), basis="unspecified", related_source_refs=card_refs or [])`.
  - If `data` is a `dict`:
    - `text`: non-empty string.
    - `basis`: string, defaults to `"unspecified"`.
    - `related_source_refs`: list of strings, defaults to `card_refs or []`.
  - Note: `"unspecified"` is an internal technical placeholder and must never be rendered as a semantic justification by the writer.

#### 1.3 `StoryCard` and Granular Normalization

- `_normalize_card_payload(raw_card, index)`:
  - Extracts canonical card fields (`id`, `topic`, `importance`, `summary`, `story_kind`, `timeframe`, `current_status`, `next_known_step`, `editorial_angle`).
  - Collects card-level `source_refs` for element inheritance.
  - Normalizes lists: `hard_facts`, `community_observations`, `useful_details`, `uncertainties`.
  - For each element in a list: attempt deserialization. If an element is invalid (empty text or missing refs), log debug/warning and drop **only that element**.
  - Checks remaining card elements: if a card has at least one valid element with valid `source_refs` (or a valid summary with card-level `source_refs`), the card is preserved.
  - If a card has no remaining valid elements and no valid refs, drop **only that card**.

#### 1.4 Granular Analysis Reference Validation (`EditorialAnalysis.validate_refs`)

- Validate every `source_ref` against `PreparedBundle.records`.
- If an individual element has an unknown ref:
  - Remove that unknown ref from the element's `source_refs`.
  - If the element has no remaining valid refs, drop that element.
- If a card has no remaining valid elements and no valid refs, drop that card.
- If at least one valid `StoryCard` remains in the analysis, return the sanitized `EditorialAnalysis`.
- Only if **all** cards are dropped does the pipeline raise `EditorialAnalysisError` to fall back.

---

### Component 2: Editorial Writer & Style Contract (`.agents/skills/news-style/SKILL.md`, `src/editorial_writer.py`)

#### 2.1 Updated News Style Guidelines

- **Article Structure**:
  - Main headline (`# ...`)
  - Lead paragraph highlighting 2–3 central topics of the 24h cycle.
  - 3–5 substantive chapters with markdown headings (`## [Тематический заголовок]`).
  - No generic categorical headings like `## Электроснабжение` or `## Прочее`. Headings must be descriptive and journalistic.
- **Synthesizing Resident Observations**:
  - Synthesizing resident reports across districts (e.g. power, water, mobile carriers, public transport) into narrative paragraphs with visible attribution (`по сообщениям жителей`, `в районных чатах отмечали`, `жители нескольких районов писали`).
  - Combining related micro-events (e.g. power cut -> search for charging -> cafe offering charging) into a continuous story of daily life.
- **Adaptive Dominance Rule**:
  - When one major event occurs, it forms an extensive main chapter (~50–60% of body), followed by 2–3 concise chapters on other urban topics.
- **Internal Chronology**:
  - Use timeline progression within a chapter when it helps explain an unfolding situation.

#### 2.2 EditorialWriter Prompt Integration

- System prompt in `EditorialWriter` enforces the 3–5 chapter composition contract, strict JSON schema output (`headline`, `lead`, `paragraphs`, `sections`), and journalistic synthesis of Story Cards + original source records.

---

### Component 3: Assistance-Aware Spam Filtering (`src/editorial_input.py`)

#### 3.1 Mutual Aid & Community Service Preservation

- In `EditorialInputBuilder._looks_commercial`:
  - Preserve messages offering or discussing mutual aid, public service, and free community assistance:
    - Keywords/patterns: `бесплатно зарядить`, `подвоз воды`, `раздача воды`, `пункт обогрева`, `помощь соседям`, `поделиться генератором`.
  - If a message contains explicit commercial/financial spam (currency exchange, loan cash-out, property sale/rent), it remains filtered.
  - The resolved `source_type` (e.g. `community`, `news`, `mixed`) is **preserved unchanged**.

---

### Component 4: Synthesis-Aware Audit (`src/editorial_audit.py`)

#### 4.1 Fact-Checker Calibration

- System prompt in `LightFactChecker` explicitly instructs:
  - **PASS / WARN**: Legitimate synthesis of collective resident discussions (e.g., comparing internet providers or reporting outages in several districts) based on source messages is valid editorial work.
  - **FIX**: Reserved strictly for:
    1. New independently verifiable claims absent from sources (invented numbers, prices, names, official orders, technical mechanisms).
    2. False causal claims (e.g., claiming a blackout was caused by an explosion when sources only report them as separate events).
    3. Severe loss of critical attribution (turning an unverified resident rumour into an established official fact).
    4. Dangerous escalation of high-risk claims (casualties, weapons, legal/financial allegations).
    5. Emotional assertions about collective psychological states (*"горожане были в панике/ярости"*) without explicit evidence in messages.

---

## Verification Plan

### Automated Tests

1. **Unit & Regression Tests (`tests/test_editorial_models.py`, `tests/test_editorial_analysis.py`)**:
   - Deserialization of string-based `hard_facts` and `community_observations` into `StoryElement(status="attributed")`.
   - Deserialization of `Uncertainty` without `basis` (defaults to `"unspecified"`).
   - Partial malformed elements / cards: one invalid ref or empty element does not drop valid cards.
   - Analysis with 1 bad card + 3 good cards preserves the 3 good cards without fallback.
2. **Filtering Tests (`tests/test_editorial_input.py`)**:
   - Verification that messages offering free gadget charging or water delivery are not filtered out by `_looks_commercial`.
   - Verification that `source_type` is never mutated by the filter.
3. **Audit Tests (`tests/test_editorial_audit.py`)**:
   - Collective synthesis is not flagged as `FIX`.
   - Invented numbers or lost attribution are flagged as `FIX`.
4. **End-to-End Pipeline Verification**:
   - Run full test suite: `uv run pytest`.
   - Run dry-run fixture simulation with realistic multi-channel dataset.
