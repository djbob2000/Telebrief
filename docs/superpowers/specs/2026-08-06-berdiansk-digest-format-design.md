# Design: Compact Single-Message Berdiansk Digest

## Goal

Make the daily digest easier to scan in Telegram by publishing the complete digest as one message with a restrained, native Telegram layout. Preserve the existing topic classification, including `Другое`, and defer prompt tuning for that group to a later change.

## Approved output format

The single published message begins with:

```text
Дайджест Бердянска · 05 августа 2026
```

Each non-empty topic group follows as a section:

```text
**📌 Предупреждения**

• Напряжение крайне нестабильно: 130–150 В вместо 220. Жителей просят не включать стиральные машины и бойлеры — техника может сгореть. [источник](https://t.me/berdiansk_me/123)

```

The final message may contain multiple sections in the existing group order. Each section has:

- a bold topic heading with its group emoji; no visible Markdown `#`;
- a blank line;
- bullet points using `•`;
- a clickable `[источник](...)` link when an original message link exists, with no channel name or `📺` attribution;
- no per-section statistics footer or time-window line.

The whole message has no separate overview/status message, no horizontal `---` separators, and no statistics footer. The existing “Другое” section remains publishable. Empty sections are omitted.

## Architecture and data flow

The change is isolated to digest formatting and delivery aggregation:

1. Existing collection, summarization, grouping, deduplication, and prompt behavior remain unchanged.
2. The formatter produces a single combined group digest with the approved title and section layout.
3. The sender sends that combined text as one Telegram message, splitting only when Telegram’s message-length limit requires it.
4. Existing cleanup tracking treats all resulting Telegram message IDs as one digest run, so a later digest removes the previous run before publishing the replacement.

The current grouped points and source URLs remain the source of truth. No new persistence format or configuration option is needed for this visual change.

## Formatting details

- Use the existing configured output language for the title and date.
- Use Telegram-compatible Markdown already supported by the sender. Topic headings should use `**...**`; links should use `[источник](url)`.
- Use the single general group marker (`📌`) for every topic heading in the approved Russian layout. Do not add per-point emojis automatically.
- Normalize section whitespace so there is one blank line between title, sections, and bullets.
- If a point has no valid source URL, omit the source suffix rather than displaying a channel label or placeholder.
- Preserve the existing length guard and make truncation retain valid Markdown as far as practical.

## Error handling

- If all groups are empty or filtered out, keep the existing no-content behavior; do not publish a blank message.
- If one section exceeds the Telegram limit, split at section or bullet boundaries when possible, preserving the same formatting.
- A source-link formatting problem must not discard the underlying bullet; omit only the invalid link.
- Existing send failures, cleanup failures, and retry/logging behavior remain unchanged.

## Testing and acceptance criteria

Update formatter and sender tests to verify:

- the title starts with `Дайджест Бердянска ·` and uses the existing Russian date format;
- the result is one combined digest string containing multiple topic sections;
- headings are bold and contain no visible `#`;
- bullets use `•`;
- source attribution contains only a clickable `источник` link and never a channel name or `📺`;
- sections contain no count or time-window footer and no `---`;
- `Другое` remains included when it has points;
- empty groups are omitted;
- sender records and sends the combined digest as one message unless length splitting is required;
- existing cleanup and Telegram length handling still pass.

Manual acceptance requires generating one real digest and confirming in `@berdiansk_news` that it is a single readable message with the approved layout and clickable source links.

## Scope exclusions

- Do not change DeepSeek prompts or classification quality in this change.
- Do not remove or reclassify `Другое` content.
- Do not redesign the Telegram channel itself.
- Do not change the daily schedule, lookback window, or source channel.
