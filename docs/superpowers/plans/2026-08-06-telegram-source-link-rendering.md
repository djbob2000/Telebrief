# Telegram Source Link Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render one compact clickable source arrow for grouped digest points, without exposing AI-generated Markdown link syntax or duplicate Telegram URLs.

**Architecture:** Keep the existing Telegram Markdown output and grouped digest flow. Add a focused normalization step in `DigestFormatter.format_group_digest` that converts Markdown-wrapped source links and raw inline URLs into clean point text, then appends the canonical `[↗](url)` link once.

**Tech Stack:** Python 3, pytest, Telegram legacy Markdown formatting.

## Global Constraints

- Existing digest structure, section headings, non-source formatting, and Telegram sending mode remain unchanged.
- Never invent URLs; use only the point's inline URL or configured `source_url`.
- A valid source URL appears at most once in the rendered point.

---

### Task 1: Lock the broken link behavior with formatter tests

**Files:**
- Modify: `tests/test_formatter.py`

**Interfaces:**
- Consumes: `DigestFormatter.format_group_digest(...)` and `GroupedPoint`.
- Produces: Regression coverage for Markdown-wrapped Telegram links and the existing raw inline URL behavior.

- [ ] **Step 1: Add a failing test for Markdown-wrapped links**

Add a test that passes this point text:

```python
f"Бердянск снова остался без света. [t.me/Brd24discord/208708]({message_url})"
```

and asserts the output contains:

```text
• Бердянск снова остался без света. [↗](https://t.me/Brd24discord/208708)
```

Also assert that `[t.me/Brd24discord/208708](` and the URL do not appear elsewhere in the result.

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
pytest -q tests/test_formatter.py -k markdown_wrapped
```

Expected: FAIL because the formatter currently leaves the Markdown wrapper/raw label in the point.

### Task 2: Normalize source links in the grouped formatter

**Files:**
- Modify: `src/formatter.py:16-18, 415-440`

**Interfaces:**
- Consumes: AI-generated point text and `GroupedPoint.source_url`.
- Produces: A grouped digest line containing clean text followed by one `[↗](url)` link.

- [ ] **Step 1: Add a Markdown-link regex alongside the existing URL regex**

Match Markdown links whose destination is an HTTPS Telegram URL and capture the visible label and URL:

```python
_MARKDOWN_SOURCE_LINK_RE = re.compile(
    r"\[([^\]]+)\]\((https://t\.me/[^)\s]+)\)"
)
```

- [ ] **Step 2: Normalize a Markdown-wrapped source before raw URL cleanup**

Inside `format_group_digest`, when a Markdown source link is found, use its destination as `source_url` and replace the whole Markdown expression with its visible label. Then run the existing inline URL cleanup and source-marker cleanup. If the visible label is only a Telegram URL, remove it so the source is represented solely by `[↗]`.

- [ ] **Step 3: Preserve the existing raw inline URL behavior**

Keep the current `https://t.me/...` extraction as the fallback for points that contain a raw URL, and continue preferring the point's inline message URL over the channel-level `source_url`.

- [ ] **Step 4: Run the focused formatter tests**

Run:

```bash
pytest -q tests/test_formatter.py
```

Expected: PASS, including existing raw inline URL regression coverage.

### Task 3: Verify the final diff

**Files:**
- Modify: `src/formatter.py`
- Modify: `tests/test_formatter.py`

- [ ] **Step 1: Run targeted static checks**

Run:

```bash
python -m compileall -q src/formatter.py
git diff --check
```

Expected: no output and exit status 0.

- [ ] **Step 2: Review the final diff and commit**

Run:

```bash
git diff -- src/formatter.py tests/test_formatter.py
git add src/formatter.py tests/test_formatter.py
git commit -m "fix: normalize grouped digest source links"
```
