# Telegram Forum Topics vs. In-Chat Replies & Dialog Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct Telegram topic handling so only real Forum Topics are split into sub-channels, while regular in-chat replies stay in a unified stream and receive inline parent context during summarization.

**Architecture:** Update `src/providers/telegram.py` to inspect `reply_to.forum_topic`, update `src/ingestion/reader.py` to only segment real forum topics, and enhance `src/summarizer.py` with inline parent quote resolution for in-chat replies.

**Tech Stack:** Python 3.14, AsyncIO, Pytest, Telethon data models.

---

### Task 1: Telegram Provider Forum Topic vs Reply Differentiation

**Files:**
- Modify: `src/providers/telegram.py:715-735`
- Test: `tests/test_telegram_provider.py`

- [ ] **Step 1: Write unit test verifying forum_topic vs reply_to separation**

```python
def test_observed_item_distinguishes_forum_topic_from_chat_reply():
    # Test that when message.reply_to.forum_topic is False, topic_id is None even if reply_to_top_id is present
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegram_provider.py -k test_observed_item_distinguishes_forum_topic_from_chat_reply --no-cov`
Expected: FAIL

- [ ] **Step 3: Update `_to_observed_item` in `src/providers/telegram.py`**

```python
is_forum_topic = getattr(reply_to, "forum_topic", False) if reply_to else False
effective_topic_id = topic_id if topic_id is not None else (reply_to_top_id if is_forum_topic else None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telegram_provider.py -k test_observed_item_distinguishes_forum_topic_from_chat_reply --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/providers/telegram.py tests/test_telegram_provider.py
git commit -m "feat(telegram): distinguish forum topics from in-chat reply chains"
```

---

### Task 2: Ingestion Reader Logical Key Resolution

**Files:**
- Modify: `src/ingestion/reader.py:90-100`
- Test: `tests/test_ingestion_reader.py`

- [ ] **Step 1: Write unit test for `_logical_channel_key` with non-forum topic IDs**

```python
def test_logical_channel_key_ignores_non_forum_replies():
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ingestion_reader.py -k test_logical_channel_key_ignores_non_forum_replies --no-cov`
Expected: FAIL

- [ ] **Step 3: Update `_logical_channel_key` in `src/ingestion/reader.py`**

Ensure `_logical_channel_key` only constructs `Channel — Topic` if `is_forum_topic` or topic is known.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ingestion_reader.py -k test_logical_channel_key_ignores_non_forum_replies --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/reader.py tests/test_ingestion_reader.py
git commit -m "feat(ingestion): prevent reply chains from fragmenting channel keys"
```

---

### Task 3: In-Chat Reply Context Formatting in Summarizer

**Files:**
- Modify: `src/summarizer.py:170-220`
- Test: `tests/test_summarizer.py`

- [ ] **Step 1: Write unit test for `format_messages_for_prompt` with reply-to context**

```python
def test_format_messages_includes_parent_reply_context():
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_summarizer.py -k test_format_messages_includes_parent_reply_context --no-cov`
Expected: FAIL

- [ ] **Step 3: Implement parent message lookup and inline quote formatting**

When formatting messages in `format_messages_for_prompt`, if a message has `reply_to_id` matching an earlier message in the batch:
Format as: `[Author (в ответ Parent: "snippet")]: text`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_summarizer.py -k test_format_messages_includes_parent_reply_context --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): enrich in-chat replies with inline parent context"
```

---

### Task 4: Full Suite Test & Live Digest Verification

**Files:**
- Test: All unit and integration tests

- [ ] **Step 1: Run full pytest suite**

Run: `uv run --all-extras pytest --no-cov`
Expected: 953+ PASS

- [ ] **Step 2: Run live digest verification**

Run `build_digest` and verify that total logical channels summarized is ~6 (not 111) and generation time is < 30 seconds.

- [ ] **Step 3: Commit and clean working tree**
