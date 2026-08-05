# Compact Single-Message Berdiansk Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current multi-message grouped digest and noisy attribution/footer with one compact, readable Telegram message.

**Architecture:** Keep collection, DeepSeek summarization, grouping, deduplication, and channel-mode formatting unchanged. Add one formatter method for the grouped digest document, make the grouped build path return that document as a single tracked part with no separate summary message, and keep the existing sender splitting/cleanup behavior as the safety net for Telegram’s length limit.

**Tech Stack:** Python 3, asyncio, python-telegram-bot Markdown parsing, pytest, existing `DigestFormatter`/`src.core`/`DigestSender` pipeline.

## Global Constraints

- Preserve the existing topic classification, including `Другое`; do not change DeepSeek prompts in this change.
- Publish the grouped digest as one message whenever it fits Telegram’s limit; split only at the existing sender limit when required.
- Use the exact Russian title pattern `Дайджест Бердянска · <Russian date>`.
- Use bold topic headings without a visible Markdown `#`.
- Use `•` bullets and clickable `[источник](url)` links without channel names or `📺` attribution.
- Do not render any per-section count or time-window footer.
- Do not publish a separate overview/status message or horizontal `---` separators.
- Preserve old-digest cleanup and message-ID tracking.
- Do not use subagents; execute this plan inline because the repository explicitly forbids subagents.

---

### Task 1: Add formatter tests for the approved single-message layout

**Files:**
- Modify: `tests/test_formatter.py`
- Test: existing `DigestFormatter` unit tests

**Interfaces:**
- Consumes: `DigestFormatter`, `GroupedPoint`, the Russian `sample_config` fixture, and the existing English fixture.
- Produces: failing tests that define `format_group_digest(grouped_sections, hours)` and its exact output rules.

- [ ] **Step 1: Write the failing tests**

Add tests covering the Russian output, source links, `Другое`, empty groups, and singular/plural counters:

```python
def test_format_group_digest_russian_compact_single_message(sample_config, mock_logger):
    formatter = DigestFormatter(sample_config, mock_logger)
    sections = [
        ("Предупреждения", [
            GroupedPoint(
                point="Напряжение 130–150 В вместо 220",
                source="Бердянск",
                source_url="https://t.me/berdiansk_me",
            ),
        ]),
        ("Другое", [
            GroupedPoint(point="Отдают котят", source="Бердянск", source_url=""),
            GroupedPoint(point="Снимут квартиру", source="Бердянск", source_url="https://t.me/berdiansk_me"),
        ]),
    ]

    result = formatter.format_group_digest(sections, hours=24)

    assert "Дайджест Бердянска ·" in result
    assert "**📌 Предупреждения**" in result
    assert "**📌 Другое**" in result
    assert "• Напряжение 130–150 В вместо 220 [источник](https://t.me/berdiansk_me)" in result
    assert "• Отдают котят" in result
    assert "Бердянск" not in result
    assert "📺" not in result
    assert "---" not in result
    assert "#" not in result
    assert "пункт" not in result
    assert "24 часа" not in result


def test_format_group_digest_omits_empty_sections_and_returns_empty_for_no_points(
    sample_config, mock_logger
):
    formatter = DigestFormatter(sample_config, mock_logger)

    result = formatter.format_group_digest(
        [("Новости", []), ("Другое", [GroupedPoint(point="Факт", source="")])],
        hours=12,
    )

    assert "Новости" not in result
    assert "**📌 Другое**" in result
    assert "12 часов" not in result
    assert formatter.format_group_digest([("Новости", [])], hours=24) == ""
```

Add an English assertion that the existing language dictionaries still control count and time wording, rather than hard-coding Russian strings inside the formatter.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
npx pytest tests/test_formatter.py -k "group_digest" -q
```

Expected: FAIL because `DigestFormatter.format_group_digest` does not exist.

- [ ] **Step 3: Commit the red tests**

```bash
git add tests/test_formatter.py
git commit -m "test: specify compact grouped digest formatting"
```

### Task 2: Implement the compact grouped formatter

**Files:**
- Modify: `src/formatter.py`
- Modify: `src/ui_strings.py` if localized compact-count strings are needed
- Test: `tests/test_formatter.py`

**Interfaces:**
- Consumes: `list[tuple[str, list[GroupedPoint]]]` and the existing `GroupedPoint.source_url` field.
- Produces: `DigestFormatter.format_group_digest(grouped_sections, hours=24) -> str`.

- [ ] **Step 1: Implement the smallest passing formatter method**

Add a method with this signature:

```python
def format_group_digest(
    self,
    grouped_sections: list[tuple[str, list[GroupedPoint]]],
    hours: int = 24,
) -> str:
```

Implement it as follows:

1. Filter out sections whose point list is empty.
2. Return `""` when no sections remain.
3. Build the title from `_format_date(datetime.now(timezone.utc))` as `Дайджест Бердянска · {date}` for Russian, with an equivalent localized title for other configured languages.
4. Render every section heading as `**📌 {group_name}**`.
5. Render each point as `• {point}` and append ` [источник](source_url)` only when `source_url` is non-empty and matches the formatter’s existing Telegram URL validation.
6. Do not render `GroupedPoint.source`, `📺`, or per-point emoji attribution.
7. Do not append a count, time-window, italic footer, or any other statistics line.
8. Join title and sections with exactly one blank line between logical blocks. Do not emit `#` or `---`.

- [ ] **Step 2: Run the focused tests and verify they pass**

Run:

```bash
npx pytest tests/test_formatter.py -k "group_digest" -q
```

Expected: all new compact-format tests pass.

- [ ] **Step 3: Run all formatter tests**

Run:

```bash
npx pytest tests/test_formatter.py -q
```

Expected: existing channel-format tests and new grouped-format tests pass.

- [ ] **Step 4: Commit the formatter**

```bash
git add src/formatter.py src/ui_strings.py tests/test_formatter.py
git commit -m "feat: format grouped digest as one compact message"
```

### Task 3: Make the grouped core path build one tracked message

**Files:**
- Modify: `src/core.py:_format_group_messages`, `_join_parts`, `_build_grouped_parts`, `_build_digest_parts`
- Modify: `tests/test_core.py`

**Interfaces:**
- Consumes: ordered grouped points from `_order_groups` and `DigestFormatter.format_group_digest`.
- Produces: the existing `Optional[tuple[list[tuple[str, str]], str]]` contract, with one tuple `("digest", combined_text)` and an empty summary string for grouped mode.

- [ ] **Step 1: Replace grouped-path tests with a failing one-message contract**

Update the grouped tests so the mocked formatter exposes `format_group_digest`, the mocked sender receives an empty summary, and the single tuple contains the entire digest:

```python
mock_formatter.format_group_digest = MagicMock(return_value="Combined digest")
mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=True)

result = await generate_and_send_digest(
    sample_config, mock_logger, hours=24, user_id=123456789
)

assert result is True
mock_formatter.format_group_digest.assert_called_once()
mock_sender.send_channel_messages_with_tracking.assert_called_once_with(
    [("digest", "Combined digest")], "", 123456789
)
```

Add a cache assertion that the stored text is exactly the combined message, without a leading blank line from an empty summary. Keep channel-mode tests unchanged.

- [ ] **Step 2: Run the focused core tests and verify they fail**

Run:

```bash
npx pytest tests/test_core.py -k "digest_mode or generate_and_send_digest_grouped or build_digest" -q
```

Expected: FAIL because the grouped core path still calls the old per-group formatter and sends a separate summary.

- [ ] **Step 3: Implement grouped aggregation**

Change `_format_group_messages` to collect ordered non-empty groups without splitting them and pass the ordered sections to `format_group_digest`. Return one tuple containing the combined text. Keep the existing `_order_groups` behavior, including placing `Другое` last.

Use the existing core return shape so channel mode remains compatible:

```python
return [("digest", combined_text)], ""
```

Update `_join_parts` to ignore empty summary text before joining. This prevents cache output from starting with `\n\n` while preserving the channel-mode summary.

- [ ] **Step 4: Run the focused core tests and verify they pass**

Run:

```bash
npx pytest tests/test_core.py -k "digest_mode or generate_and_send_digest_grouped or build_digest" -q
```

Expected: all grouped and channel-mode tests selected by the filter pass.

- [ ] **Step 5: Commit the core aggregation**

```bash
git add src/core.py tests/test_core.py
git commit -m "feat: send grouped digest as one tracked message"
```

### Task 4: Remove obsolete grouped-message assumptions and verify sender behavior

**Files:**
- Modify: `src/formatter.py` and `tests/test_formatter.py` only if the old grouped formatter methods are no longer referenced
- Modify: `src/sender.py` docstrings/log wording if they still claim every tracked part is a channel message
- Modify: `tests/test_sender.py` if tracking assertions need the neutral `digest` part name

**Interfaces:**
- Consumes: the existing `send_channel_messages_with_tracking` tracking path with `summary_message=""`.
- Produces: tracked message IDs for one combined digest, with existing split behavior for messages over 4000 characters.

- [ ] **Step 1: Search for stale grouped formatter callers**

Run:

```bash
rg -n "format_group_message|format_group_summary_message|summary_message|channel_messages" src tests
```

Remove only dead grouped-format methods and tests after confirming channel mode does not use them. Keep `format_channel_message`, `format_summary_message`, and sender methods needed by `digest_mode: channel`.

- [ ] **Step 2: Add or update sender tracking tests**

Verify that an empty summary sends no placeholder and that the combined digest is sent once and its returned message ID is passed to `save_digest_message_ids`. Preserve the existing long-message test to prove the sender still splits only when necessary.

- [ ] **Step 3: Run sender and core regression tests**

Run:

```bash
npx pytest tests/test_sender.py tests/test_core.py -q
```

Expected: zero failures.

- [ ] **Step 4: Commit cleanup and regression coverage**

```bash
git add src/formatter.py src/sender.py tests/test_formatter.py tests/test_sender.py
git commit -m "test: preserve digest tracking and length splitting"
```

### Task 5: Run the complete verification and perform manual Telegram acceptance

**Files:**
- No source changes expected; modify tests only if a verified regression exposes a concrete mismatch with the approved spec.

**Interfaces:**
- Consumes: the completed formatter/core/sender pipeline and the existing local Telegram deployment.
- Produces: test evidence and one real compact digest in `@berdiansk_news`.

- [ ] **Step 1: Run the complete automated test suite**

Run:

```bash
docker run --rm -v "$PWD:/app" -w /app telebrief:local sh -c 'pip install -q -r requirements-dev.txt && pytest -q'
```

Expected: zero failures; record the final passed/skipped counts.

- [ ] **Step 2: Validate configuration and restart the local service**

Run:

```bash
docker compose config >/dev/null
docker compose up -d
docker compose ps
```

Expected: `telebrief` is `healthy` and the configured schedule remains unchanged.

- [ ] **Step 3: Trigger one real digest**

Send `/digest` to `@berdiansk_news_digest_bot` as the configured owner and wait for the service logs to report successful publication. Confirm that the destination contains one digest message unless Telegram length splitting is unavoidable.

- [ ] **Step 4: Inspect the published message**

Confirm all of the following in `@berdiansk_news`:

- title is `Дайджест Бердянска · 05 августа 2026`-style Russian date output;
- each section heading is bold with `📌`, with no visible `#`;
- points use `•`;
- only clickable source links remain, with no `📺 Бердянск` labels;
- sections have no count or time-window footer;
- there is no `---` and no separate summary message;
- `Другое` remains present when the grouper returns points for it.

- [ ] **Step 5: Commit any final test-only adjustment and report evidence**

If no adjustment is needed, leave the worktree clean and report the exact test command, container health, and manual Telegram result.
