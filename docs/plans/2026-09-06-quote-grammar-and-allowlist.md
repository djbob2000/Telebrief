# Quote Grammar Preservation and Robust Allowlist Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore correct Russian grammatical formatting for resident quotes by improving primary-source quote allowlist extraction, preventing crude quotation mark stripping after colons, and fixing `article_2026-09-06.md`.

**Architecture:** 
1. `build_article_quote_allowlist` (`src/publication/article_quote_allowlist.py`) becomes token-tolerant with minor typo and punctuation awareness and extracts authentic community statements directly from primary `source_text`.
2. `_strip_non_allowlisted_quotes` (`src/publication/article_models.py`) checks token matching against primary sources before stripping, and when stripping is necessary, transforms direct speech after colons (`: «...»`) into valid indirect speech (`, что ...`) rather than leaving an unquoted direct speech error.
3. `ArticleEditor` prompt (`src/publication/article_editor.py`) explicitly forbids colons without quotes.
4. Correct quotes in `article_2026-09-06.md` and verify against the full test suite.

**Tech Stack:** Python 3.12+, regex, pytest, Levenshtein token matching.

---

### Task 1: Token-Tolerant Quote Token Matching in `article_claims.py`

**Files:**
- Modify: `src/publication/article_claims.py:240-260`
- Test: `tests/publication/test_article_validator.py`

- [ ] **Step 1: Write unit tests for typo-tolerant quote token matching**

Add to `tests/publication/test_article_validator.py`:
```python
def test_quote_tokens_match_tolerates_minor_typos_and_spacing():
    from src.publication.article_claims import _quote_tokens_match

    # Normalization & punctuation
    assert _quote_tokens_match(
        "Звук генераторов уже как колыбельная перед сном",
        "Звук генераторов , уже как колыбельная перед сном...",
    )
    # 1-char typo tolerance for long words (>=6 chars)
    assert _quote_tokens_match(
        "Че там по свету в Бердянске? Все стабильно? Стабильно отсутствует",
        "Че там по свету в Бердянске? Все стабильно? Стабильно отсутсвует?",
    )
    # Different wording must not match
    assert not _quote_tokens_match(
        "Света не будет до зимы",
        "Свет обещали включить завтра",
    )
```

- [ ] **Step 2: Run test to verify it fails on typos**

Run: `.venv/bin/pytest tests/publication/test_article_validator.py -k "test_quote_tokens_match_tolerates_minor_typos_and_spacing" -v`
Expected: FAIL (returns False on `отсутствует` vs `отсутсвует`)

- [ ] **Step 3: Implement typo tolerance in `_quote_tokens_match`**

In `src/publication/article_claims.py`:
Implement `_levenshtein_distance` and use it in `_quote_tokens_match`:
```python
def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if not s2:
        return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            ins = prev[j + 1] + 1
            dels = curr[j] + 1
            subs = prev[j] + (c1 != c2)
            curr.append(min(ins, dels, subs))
        prev = curr
    return prev[-1]


def _words_match(w1: str, w2: str) -> bool:
    if w1 == w2:
        return True
    if len(w1) >= 6 and len(w2) >= 6 and abs(len(w1) - len(w2)) <= 1:
        return _levenshtein_distance(w1, w2) <= 1
    return False


def _quote_tokens_match(quote_text: str, source_text: str) -> bool:
    """Check if words in quote_text form a contiguous subsequence of words in source_text, with minor typo tolerance."""
    q_words = quote_words(quote_text)
    if not q_words:
        return False
    s_words = quote_words(source_text)
    if len(s_words) < len(q_words):
        return False
    q_len = len(q_words)
    for i in range(len(s_words) - q_len + 1):
        if all(_words_match(qw, sw) for qw, sw in zip(q_words, s_words[i : i + q_len])):
            return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_article_validator.py -k "test_quote_tokens_match_tolerates_minor_typos_and_spacing" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/publication/article_claims.py tests/publication/test_article_validator.py
git commit -m "fix(claims): add typo and punctuation tolerance to quote token matching"
```

---

### Task 2: Robust Quote Allowlist Extraction (`build_article_quote_allowlist`)

**Files:**
- Modify: `src/publication/article_quote_allowlist.py`
- Test: `tests/publication/test_article_writer_prompt.py`

- [ ] **Step 1: Write unit tests for robust allowlist builder**

Update `tests/publication/test_article_writer_prompt.py`:
```python
def test_build_article_quote_allowlist_extracts_primary_quotes_and_filters_trivial():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    s_spaced = ArticleSupport(
        support_id="story:1:evidence:0:frag:1",
        text="Звук генераторов, уже как колыбельная перед сном...",
        source_text="Звук генераторов , уже как колыбельная перед сном...",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:1",
    )
    s_trivial = ArticleSupport(
        support_id="story:1:evidence:1:frag:2",
        text="нет",
        source_text="нет",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:1",
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Энергетика",),
        support_index=(s_spaced, s_trivial),
        support_by_id={s.support_id: s for s in (s_spaced, s_trivial)},
        recurring_topics=(),
    )

    allowlist = build_article_quote_allowlist(ctx)
    assert any("Звук генераторов" in q for q in allowlist)
    assert "нет" not in allowlist
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_article_writer_prompt.py -k "test_build_article_quote_allowlist_extracts_primary_quotes_and_filters_trivial" -v`
Expected: FAIL

- [ ] **Step 3: Implement robust allowlist extraction**

In `src/publication/article_quote_allowlist.py`:
```python
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.publication.article_claims import _quote_tokens_match, quote_words
from src.publication.article_writer_context import sanitize_writer_source_text

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext

_TRIVIAL_WORDS = frozenset({"да", "нет", "ок", "не", "хорошо", "точно", "плюс", "плюсую"})


def build_article_quote_allowlist(
    context: ArticleEditorialContext,
) -> tuple[str, ...]:
    """Extract verifiable verbatim primary-source snippets eligible for direct quotation marks."""
    allowlist: list[str] = []
    seen: set[str] = set()

    def _add_cand(cand: str, source_text: str) -> None:
        c = cand.strip(" .,!?;:-—\t\n\r«»\"“”")
        if not c:
            return
        tokens = quote_words(c)
        if len(tokens) < 2:
            return
        if all(t in _TRIVIAL_WORDS for t in tokens):
            return
        if sanitize_writer_source_text(c) != c:
            return
        if not _quote_tokens_match(c, source_text):
            return
        if c not in seen:
            seen.add(c)
            allowlist.append(c)

    for sup in context.support_index:
        if sup.publication_use != "PUBLISH":
            continue
        if sup.evidence_kind == "resident_question":
            continue
        if sup.support_kind == "operational":
            continue

        # 1. Candidate from sup.text
        if sup.text:
            _add_cand(sup.text, sup.source_text)

        # 2. Extract clean sentences from sup.source_text for community reports and established facts
        if sup.source_text and sup.evidence_kind in (
            "community_report",
            "established_fact",
            "official_statement",
        ):
            raw_lines = re.split(r"[\r\n]+", sup.source_text)
            for line in raw_lines:
                line = line.strip()
                if 10 <= len(line) <= 150:
                    _add_cand(line, sup.source_text)

    return tuple(allowlist)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_article_writer_prompt.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/publication/article_quote_allowlist.py tests/publication/test_article_writer_prompt.py
git commit -m "fix(quotes): make quote allowlist extraction token-tolerant and filter trivial words"
```

---

### Task 3: Grammatically Sound Quote Stripping (`_strip_non_allowlisted_quotes`)

**Files:**
- Modify: `src/publication/article_models.py:130-165`
- Test: `tests/publication/test_article_models.py` (or new test in `test_article_validator.py`)

- [ ] **Step 1: Write test for grammatical quote stripping**

Add to `tests/publication/test_article_validator.py`:
```python
def test_strip_non_allowlisted_quotes_converts_direct_speech_to_indirect():
    from src.publication.article_models import _strip_non_allowlisted_quotes

    allowlist = ["Звук генераторов уже как колыбельная перед сном"]

    # Allowed quote preserves colon and quotes
    text_allowed = "Житель признался: «Звук генераторов уже как колыбельная перед сном», а другой подтвердил."
    res_allowed = _strip_non_allowlisted_quotes(text_allowed, allowlist)
    assert res_allowed == "Житель признался: «Звук генераторов уже как колыбельная перед сном», а другой подтвердил."

    # Unallowed quote converts to indirect speech without leaving naked colon
    text_unallowed = "Житель признался: «Света не будет до конца года», а другой подтвердил."
    res_unallowed = _strip_non_allowlisted_quotes(text_unallowed, allowlist)
    assert res_unallowed == "Житель признался, что света не будет до конца года, а другой подтвердил."
    assert "признался:" not in res_unallowed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/publication/test_article_validator.py -k "test_strip_non_allowlisted_quotes_converts_direct_speech_to_indirect" -v`
Expected: FAIL

- [ ] **Step 3: Implement safe direct speech transformation in `_strip_non_allowlisted_quotes`**

In `src/publication/article_models.py`:
```python
def _strip_non_allowlisted_quotes(text: str, quote_allowlist: Sequence[str] | None = None) -> str:
    """Normalize non-allowlisted quotation marks into indirect speech without quotes.

    Quotes matching allowlisted exact phrases or word subsequences are preserved.
    Direct speech preceded by a colon (: «...») is converted into indirect speech (, что ...)
    to avoid leaving unquoted direct speech after colons.
    """
    if not text or ("«" not in text and '"' not in text and "“" not in text):
        return text

    from src.publication.article_claims import _quote_tokens_match

    allowlist_norm = {
        re.sub(r"\s+", " ", q.lower().replace("ё", "е")).strip()
        for q in (quote_allowlist or ())
        if q
    }
    raw_allowlist = [q for q in (quote_allowlist or ()) if q]

    def _is_allowed(inner: str) -> bool:
        norm_inner = re.sub(r"\s+", " ", inner.lower().replace("ё", "е")).strip()
        if allowlist_norm and any(
            norm_inner == a or norm_inner in a or a in norm_inner for a in allowlist_norm
        ):
            return True
        if raw_allowlist and any(_quote_tokens_match(inner, a) for a in raw_allowlist):
            return True
        return False

    def _repl_colon(m: re.Match[str]) -> str:
        quote = m.group(1).strip()
        if _is_allowed(quote):
            return m.group(0)
        lowered = quote[0].lower() + quote[1:] if quote else quote
        return f", что {lowered}"

    def _repl_normal(m: re.Match[str]) -> str:
        quote = m.group(1).strip()
        if _is_allowed(quote):
            return m.group(0)
        return quote

    # 1. Transform speech lead-in quotes with colons: : «...» -> , что ...
    res = re.sub(r":\s*[«\"“]([^»\"”]{2,120})[»\"”]", _repl_colon, text)
    # 2. Transform remaining quoted terms
    res = re.sub(r"[«\"“]([^»\"”]{2,120})[»\"”]", _repl_normal, res)
    return res
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/publication/test_article_validator.py -k "test_strip_non_allowlisted_quotes_converts_direct_speech_to_indirect" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/publication/article_models.py tests/publication/test_article_validator.py
git commit -m "fix(article_models): convert unallowed quotes after colons into indirect speech"
```

---

### Task 4: Clarify ArticleEditor Prompt Instructions

**Files:**
- Modify: `src/publication/article_editor.py:255-265`

- [ ] **Step 1: Update prompt in `_build_system_prompt`**

In `src/publication/article_editor.py`:
Update Rule 1:
```python
            "1. ПРЯМАЯ РЕЧЬ И КАВЫЧКИ (UNSUPPORTED_DIRECT_QUOTE):\n"
            "   - Запрещено оставлять кавычки «...» вокруг слов или фраз, если они не являются 100% дословной цитатой из предоставленных фактов.\n"
            "   - Переведите фразу в естественную косвенную речь БЕЗ КАВЫЧЕК через союз «что» со строчной буквы (например: «житель признался, что...», «горожане отмечают, что...»).\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО оставлять двоеточие перед текстом без кавычек (например: «житель признался: Звук генераторов...» — это грубая грамматическая ошибка).\n\n"
```

- [ ] **Step 2: Run test suite to verify no regressions**

Run: `.venv/bin/pytest tests/publication/test_article_editor.py -v` (if exists, or all publication tests)

- [ ] **Step 3: Commit**

```bash
git add src/publication/article_editor.py
git commit -m "docs(editor): add explicit prohibition of colons before unquoted direct speech"
```

---

### Task 5: Correct Quotes in `article_2026-09-06.md` & Full Validation

**Files:**
- Modify: `article_2026-09-06.md`

- [ ] **Step 1: Update quotes in `article_2026-09-06.md`**

In `article_2026-09-06.md`:
1. Line 9:
   - Change `обещания в понедельник будет свет` to `обещания «в понедельник будет свет»`
   - Change `констатируют: Че там по свету в Бердянске? Все стабильно? Стабильно отсутствует.` to `констатируют: «Че там по свету в Бердянске? Все стабильно? Стабильно отсутствует».`
2. Line 17:
   - Change `признался: Звук генераторов уже как колыбельная перед сном,` to `признался: «Звук генераторов уже как колыбельная перед сном»,`

- [ ] **Step 2: Validate `article_2026-09-06.md` with publication validator**

Run Python script against run 18 frozen input to verify 0 blocking violations:
```bash
.venv/bin/python -c "
import asyncio
from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.article_validator import validate_article_draft
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_quote_allowlist import build_article_quote_allowlist

# read article_2026-09-06.md, parse draft, validate
"
```
Expected: `is_valid: True, blocking_violations: []`

- [ ] **Step 3: Run full publication test suite**

Run: `.venv/bin/pytest tests/publication/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add article_2026-09-06.md
git commit -m "fix(article): restore proper resident quotation punctuation in 2026-09-06 article"
```
