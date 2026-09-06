# Design Spec: Quote Grammar Preservation and Robust Allowlist Extraction

## 1. Problem Statement & Motivation

During editorial article generation, the model-writer generates authentic resident quotes with correct Russian punctuation, such as:
- `...один из жителей признался: «Звук генераторов уже как колыбельная перед сном», а другой...`
- `...а третьи констатируют: «Че там по свету в Бердянске? Все стабильно? Стабильно отсутствует».`

However, in the published article (`article_2026-09-06.md`), quotation marks disappeared, leaving broken grammar:
- `...один из жителей признался: Звук генераторов уже как колыбельная перед сном, а другой...`
- `...а третьи констатируют: Че там по свету в Бердянске? Все стабильно? Стабильно отсутствует.`

### Root Cause Analysis
1. **Punctuation & Typos in `build_article_quote_allowlist` (`src/publication/article_quote_allowlist.py`)**:
   `build_article_quote_allowlist` uses a strict Python substring check: `if cand not in sup.source_text: continue`.
   In raw community messages, extra whitespace around punctuation (e.g. `"генераторов , уже"` in `source_text` vs `"генераторов, уже"` in normalized `text`) or slight source typos (e.g. `"отсутсвует"` in `source_text` vs `"отсутствует"` in `text`) caused genuine resident statements to be discarded from the allowlist.
   Furthermore, candidate extraction only looked at `sup.text`. If `sup.text` was an editorial summary, the authentic resident quote residing in `sup.source_text` was never extracted.
   Conversely, single-word fragments like `"нет"` slipped into the allowlist and acted as wildcard substring matches.

2. **Crude Quote Stripping in `_strip_non_allowlisted_quotes` (`src/publication/article_models.py`)**:
   The regex replacement in `_strip_non_allowlisted_quotes` unconditionally stripped quotes `«` and `»` if an exact allowlist match was not found. When applied to direct speech after a colon (`признался: «...»`), it created a direct speech construction without quotation marks, which is a grammatical violation in Russian.

3. **Editor Prompt Ambiguity in `ArticleEditor` (`src/publication/article_editor.py`)**:
   The prompt instructed the editor to convert unverified quotes to indirect speech without quotes, but did not explicitly prohibit leaving colons before unquoted direct speech (`: Текст без кавычек`).

---

## 2. Proposed Architecture & Changes

### Component 1: Robust Quote Allowlist Builder (`src/publication/article_quote_allowlist.py`)
- **Token-Aware & Punctuation-Tolerant Matching**:
  When checking if `sup.text` is backed by `sup.source_text`, use token sequence matching (`_quote_tokens_match` from `article_claims.py` or normalized token comparison) instead of rigid character substring equality. This tolerates spacing around commas/dashes, capitalization, and minor typos.
- **Extracting Primary Quotes from `sup.source_text`**:
  For substantive `PUBLISH` supports (`community_report`, `established_fact`), extract clean sentence-level or clause-level resident statements directly from `sup.source_text` when sanitized.
- **Trivial Word Filtering**:
  Filter out isolated monosyllabic or trivial single-word responses (e.g. length < 3 words or words in a trivial exclusion set like `"нет"`, `"да"`, `"ок"`), preventing them from polluting the allowlist or acting as substring wildcards.

### Component 2: Safe Quote Stripping & Grammar Normalization (`src/publication/article_models.py`)
- **Direct Speech Transformation**:
  When `_strip_non_allowlisted_quotes` processes a quoted span:
  - First, check if the inner text matches the allowlist OR matches any cited support source text via `_quote_tokens_match`. If it matches an authentic resident quote, preserve `«...»`.
  - If it is truly an unverified quote that must be converted to indirect speech:
    - If preceded by `: ` (e.g., `признался: «Цитата»` or `констатируют: «Цитата»`), replace the colon and quote with indirect conjunction syntax (`, что <текст со строчной буквы>`), preventing unquoted direct speech after colons.
    - Otherwise, strip the quotation marks smoothly without leaving orphan punctuation.

### Component 3: Copy-Editor Prompt Instruction (`src/publication/article_editor.py`)
- Update rule 1 in `ArticleEditor._build_system_prompt`:
  Explicitly state that direct speech after colons must either use exact quotation marks `«...»` from primary sources or be transformed into indirect speech via `, что ...` with a lowercase initial letter. Colons without quotation marks are strictly forbidden.

### Component 4: Published Article Correction (`article_2026-09-06.md`)
- Restore direct quotation marks in `article_2026-09-06.md` for the two confirmed authentic community quotes in lines 9 and 17:
  - `...а третьи констатируют: «Че там по свету в Бердянске? Все стабильно? Стабильно отсутствует».`
  - `...один из жителей признался: «Звук генераторов уже как колыбельная перед сном», а другой...`
  - Also wrap `«в понедельник будет свет»` in quotation marks in line 9.

---

## 3. Verification Plan

### Automated Tests
1. **Unit tests in `tests/publication/test_article_writer_prompt.py`**:
   - Verify `build_article_quote_allowlist` handles spacing around punctuation (`"генераторов , уже"` vs `"генераторов, уже"`).
   - Verify extraction of authentic primary source statements from `source_text`.
   - Verify exclusion of trivial 1-word tokens (`"нет"`).
2. **Unit tests for `_strip_non_allowlisted_quotes` in `tests/publication/test_article_models.py` (or new test file)**:
   - Verify authentic quote preservation when tokens match.
   - Verify grammatical transformation from `: «...»` to `, что ...` when stripping is required.
3. **Full Publication Test Suite**:
   - Run `pytest tests/publication/` to ensure no regressions in validation, eligibility, or coverage.

### Validation on Current Article
- Run `validate_article_draft` on the patched `article_2026-09-06.md` using the updated allowlist logic to ensure 0 blocking validation issues.
