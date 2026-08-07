# Rich Digest Formatting Implementation Plan

**Goal:** Send the combined Berdyansk digest through Telegram Rich Messages with native headings, unordered lists, and compact clickable source arrows.

**Architecture:** Keep the existing Markdown formatter as a compatibility path, add a structured Rich Message document builder for grouped digests, and add a small HTTP adapter in the sender for the Bot API sendRichMessage method. Rich sending returns Telegram message IDs and falls back to Markdown only when the Rich Message request fails.

**Tech Stack:** Python 3, aiohttp, python-telegram-bot 22.6, Telegram Bot API 10.1+, pytest, Docker Compose.

## Global Constraints

- Use native heading blocks for the digest title and group names.
- Use native unordered list blocks for news items.
- Render the source as one clickable ↗ link at the end of each item.
- Preserve message-ID cleanup and Markdown fallback.
- Keep local /Users/air/develop/Telebrief and remote /home/opc/Telebrief synchronized.

## Tasks

1. Add failing formatter and sender tests for Rich Message blocks, links, returned message IDs, and fallback.
2. Implement structured Rich Message formatting and block-aware splitting.
3. Implement the Bot API HTTP adapter and route combined digest sending through it.
4. Run the full test suite and Docker verification.
5. Deploy the same source to Oracle and verify health and logs.
