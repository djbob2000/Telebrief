# Design Specification: Telegram Forum Topics vs. In-Chat Replies & Dialog Enrichment

## 1. Problem Statement

In the current ingestion and reading pipeline:
1. **False Topic Fragmentation:** In `src/providers/telegram.py`, `effective_topic_id` fell back to `reply_to_top_id` regardless of whether the message was from a Telegram Forum topic (`forum_topic == True`) or a regular in-chat reply in an open discussion group (`forum_topic == False`).
2. **Context Loss & LLM Bloat:** Regular chat messages with replies were fragmented into 100+ virtual pseudo-channels (e.g. `Бердянск — 6969928`), causing 100+ LLM summarization calls on isolated 1-2 line snippets, losing conversational context (question asked vs answered) and inflating generation time and token costs.

## 2. Goals & Success Criteria

- **Distinct Forum Topics:** Only genuine Telegram Forum Topics (`reply_to.forum_topic is True` or configured channel topics) are split into logical sub-channels.
- **Unified Chat Streams with Contextual Replies:** Regular group chats remain a single unified channel message stream.
- **Inline Reply Enrichment:** When messages in a chat reference parent messages via `reply_to_id`, the prompt formatter preserves the conversational link (`[Author (в ответ Parent: "snippet")]: text`), giving the LLM immediate conversational context.
- **Performance:** Reduces digest channel count from 110+ down to ~6 logical channels, accelerating digest generation by 5x–8x while improving factual coherence.

## 3. Architecture & Component Changes

### 3.1 Telegram Provider (`src/providers/telegram.py`)
- Check `message.reply_to`:
  - If `reply_to and getattr(reply_to, "forum_topic", False)`:
    - `effective_topic_id = topic_id if topic_id is not None else getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)`
  - Else:
    - `effective_topic_id = topic_id` (set only if fetching an explicit configured forum topic).
- Store `reply_to_id = getattr(reply_to, "reply_to_msg_id", None)` for all replies.

### 3.2 Reader & Logical Key Resolution (`src/ingestion/reader.py`)
- `_logical_channel_key`:
  - When grouping messages by channel, only append `— {topic_id}` if the source channel is a configured forum channel or if `topic_id` represents an actual forum topic (not an ad-hoc reply thread).
  - Regular open chat messages stay consolidated under `source.name`.

### 3.3 Prompt Message Formatter (`src/summarizer.py` & `src/editorial_input.py`)
- In `format_messages_for_prompt`:
  - Build a lookup map of `message_id -> (author, text_snippet)` for messages within the channel batch.
  - When formatting a message that has `reply_to_id` matching an earlier message in the batch, include the inline parent context:
    - `[Автор (в ответ Родителю: "фрагмент исходного сообщения")]: текст ответа`
  - If parent message is outside the lookback window or missing, render cleanly without breaking.

## 4. Verification Plan

1. **Unit Tests:**
   - Test `_to_observed_item` with `forum_topic=True` vs `forum_topic=False` to verify `topic_id` is only set for genuine forum topics.
   - Test `format_messages_for_prompt` with reply chains to verify inline quoted dialog formatting.
   - Run full test suite: `uv run --all-extras pytest --no-cov`.
2. **Live Verification:**
   - Execute `build_digest(config, logger, hours=24)` and verify that channel count is ~6 (not 111) and generation time is under 30 seconds.
