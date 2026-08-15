# Telebrief: editorial Story Cards and resilient daily article pipeline

**Status:** ready for implementation planning

## Goal

Turn the daily article into a readable local-news story about the last 24 hours in Berdyansk. The system should synthesize a large, heterogeneous Telegram stream without inventing concrete facts, and should still produce a useful thematic publication when an AI stage is unavailable.

The article workflow is separate from the existing short digest. It may use long-form structure, natural transitions, community observations, and an editorial angle. Accuracy controls protect against unsupported concrete claims without forcing every sentence through an atomic claim registry.

## Non-goals

- Do not make the article a list of raw Telegram posts.
- Do not require one claim ID per sentence or paragraph.
- Do not treat source type as a trust score or publication permission.
- Do not make batching the normal editorial architecture.
- Do not publish a raw “latest messages” dump as an emergency article.
- Do not make a failed audit suppress an otherwise usable article unless an unsafe high-risk assertion cannot be repaired.

## 1. Source roles and configuration

Add an optional `source_type` to `ChannelConfig` and to forum-topic configuration. Allowed values:

- `news`: short news and editorial reporting; factual frame for a story;
- `community`: resident observations, repetition, district-level signals, discussion and practical consequences;
- `official`: the organization’s own decisions, schedules, addresses, times and actions;
- `classifieds`: low-priority commercial and advertising material;
- `mixed`: no fixed role; classify each message or local context by content.

The field is an editorial prior, not a measure of truth. A `news` message still requires appropriate attribution and wording; a `community` observation can be important but remains an observation; `official` is not inferred from a channel name.

Effective role precedence is:

```text
topic.source_type -> channel.source_type -> mixed
```

Missing `source_type` remains valid and resolves to `mixed`. Invalid values fail configuration validation. Existing configurations without the field continue to load unchanged.

The current configuration explicitly sets:

- `Бердянск` (`@berdiansk_me`) to `community`;
- `Новости Бердянска` topic to `news`;
- `Проблемы ЖКХ` topic to `news` (not `official` unless the source is the actual utility/administration);
- all three numeric-ID groups to `mixed`.

## 2. Message preparation and context

Extend `Message` with optional, backwards-compatible metadata:

```python
message_id: int | None = None
reply_to_id: int | None = None
topic_id: int | None = None
```

The collector fills these fields when Telegram provides them. Existing constructors and tests remain valid through defaults.

Each prepared source receives a stable internal reference such as `S000123`, retained across the normal path, batching, merge, Story Cards, audit and repair. The reference maps back to the original raw `Message`; excerpts and AI summaries are not the source of truth. The reference is assigned before any AI processing and maps to a composite identity such as chat/channel plus topic plus Telegram `message_id`; `S######` is unique at least within one article run because Telegram IDs are only chat-local.

The prepared representation includes message text, channel/topic, effective source role, timestamp, sender, link, media information and available reply/topic metadata. For a short or context-dependent message, include the parent message text when available and a small selective neighboring context. Do not duplicate broad neighborhoods for every message.

Prefiltering is conservative. It may remove empty service records, emoji-only items, obvious commercial spam, currency ads and explicit advertising. It must retain short messages that contain a district, object, time, problem or action, or that become meaningful through reply/topic context. It must not attempt to solve the editorial task with aggressive regex rules.

The primary path prepares the largest bundle supported by the selected model’s context window, reserving space for the system prompt and the editorial-analysis response. It uses the complete 24-hour collection when the provider accepts it. The current per-channel safety ceiling remains 5000; it is a transport safeguard, not a normal editorial truncation target.

The full-bundle request first passes through the existing provider cascade. If any configured provider/model accepts the full bundle, batching is not used. Only when the available variants explicitly reject the request for context length or request size may the system split the input into semantic/time/source-aware batches, analyze each, and run one merge pass. A normal timeout does not imply an oversized context: provider cascade/retry handling runs first. Batching must preserve source references and must not change editorial semantics.

## 3. Editorial analysis and Story Cards

The first AI stage is called `editorial_analysis`, `daily_story_analysis` or `story_extraction`, not claim extraction. It reads the prepared message bundle and identifies the day’s stories. It should:

- ignore obvious advertising, currency notices, commercial spam, chatter and context-free filler;
- group messages about the same event or recurring condition;
- preserve concrete details, districts, timeframes and useful services;
- distinguish news/official material from resident observations and questions;
- use repetition as one signal, not as a vote or automatic proof of scale;
- rank by public significance, specificity, geographic reach, practical impact, novelty and source mix;
- keep independent events separate unless the relationship is explicitly supported.

The output is a flexible collection of Story Cards. Usually there are 4–12 cards, but this is only an orientation: a quiet day may have fewer and a dense day may have more. No card is created to fill a quota.

The card vocabulary is intentionally small and explicit:

- `importance`: `high | medium | low`;
- item `status`: `established | attributed | disputed`;
- `story_kind`: optional free-form editorial category, not a closed enum.

Each card contains a stable ID, topic, importance, summary, optional timeframe/current status/next known step, and an `editorial_angle` object:

```json
{
  "text": "Электроснабжение стало одной из заметных бытовых тем дня.",
  "basis_refs": ["S000031", "S000074", "S000182"],
  "type": "editorial_synthesis"
}
```

`editorial_angle` is a supported editorial synthesis, not an established fact. Card elements keep their own references:

```json
{
  "id": "STORY-001",
  "topic": "Электроснабжение",
  "importance": "high",
  "story_kind": "infrastructure",
  "summary": "Перебои с электричеством обсуждались жителями нескольких районов.",
  "timeframe": "вечер 14 августа",
  "current_status": "",
  "next_known_step": "",
  "editorial_angle": {
    "text": "Электроснабжение стало одной из заметных бытовых тем дня.",
    "basis_refs": ["S000031", "S000074", "S000182"],
    "type": "editorial_synthesis"
  },
  "hard_facts": [
    {
      "text": "Коммунальное предприятие сообщило ...",
      "source_refs": ["S000184"],
      "attribution": "КП ...",
      "status": "established"
    }
  ],
  "community_observations": [
    {
      "text": "Жители нескольких районов сообщали об отсутствии света.",
      "source_refs": ["S000031", "S000074"],
      "areas": ["АКЗ", "Колония"],
      "status": "attributed"
    }
  ],
  "useful_details": [],
  "uncertainties": [
    {
      "text": "Причина перебоев по доступным сообщениям не установлена.",
      "basis": "No supplied source directly establishes the cause",
      "related_source_refs": ["S000031", "S000184"]
    }
  ]
}
```

The `status` of an item depends on both wording and source role. A named organization’s own announcement may establish its own action; a resident’s report about that announcement remains attributed. Uncertainty records describe the evidence boundary and must not pretend that related sources prove an absence of information.

Message-level labels are retained independently of channel role. A message may have one primary label plus flags, for example:

```json
{"label": "community_observation", "flags": ["question", "location_specific"]}
```

Possible labels include `news_item`, `community_observation`, `official_notice`, `question`, `advertising` and `chatter`.

## 4. Writer

The writer receives Story Cards, relevant original excerpts selected by `source_refs`, source metadata and the news-style skill. Story Cards are reporting notes, not a sentence template:

> Combine, reorder, compress and connect their material naturally. Do not mechanically reproduce card fields or source order.

The writer produces a professional Russian local-news article with an informative headline, lead, natural transitions and H2 sections when the material supports genuinely distinct sections. In a dense day, 900–1800 words is an orientation, not a minimum or hard cap. A thin day should be short rather than padded.

Community material is written as attributed observation or discussion (“жители сообщали”, “в чатах обсуждали”). News and official material retain the appropriate attribution and modality. The writer may synthesize, prioritize and narrate supplied material, including an editorial angle, but may not create a new independently verifiable fact absent from the Story Cards and referenced source messages.

The writer must not infer a technical mechanism, cause, sales trend, comparison, official action, damage, responsibility, casualty, medical/legal conclusion or precise scale merely because it would make the narrative smoother. It must preserve contradictions instead of choosing a winner without support.

## 5. Light fact check and targeted repair

Fact checking is a separate AI call. It evaluates the article against Story Cards and the original messages, allowing cautious synthesis and normal journalistic transitions. Paragraphs receive internal IDs (`P001`, `P002`, …) before audit; these IDs are removed before publication.

The audit returns:

```json
{
  "status": "PASS | WARN | FIX",
  "systemic_problem": false,
  "issues": [
    {
      "severity": "fix",
      "code": "UNSUPPORTED_SALES_CLAIM",
      "paragraph_id": "P004",
      "original_excerpt": "Продажи генераторов резко выросли.",
      "reason": "Источники подтверждают обсуждение генераторов, но не продажи.",
      "suggested_direction": "Описать обсуждение выбора и использования генераторов.",
      "source_refs": ["S000031", "S000074"]
    }
  ]
}
```

Issue references identify material to check, not proof that the referenced sources support the writer’s wording. The overall status is deterministic: any `FIX` means `FIX`; otherwise any `WARN` means `WARN`; no issues means `PASS`.

`WARN` is non-blocking. It covers a soft overstatement or debatable transition without a new concrete fact. `FIX` is reserved for a materially unsupported, independently checkable assertion, lost attribution, or dangerous high-risk wording. Three to five local fixes are not systemic by themselves.

For `FIX`, a repair call receives the paragraph and nearby context, the issue, relevant Story Cards and the original messages. It must return only the replacement fragment and preserve purpose, tone and narrative:

> Change only what is necessary to remove the unsupported assertion. Do not introduce replacement facts absent from the supplied Story Cards or original messages.

At most two targeted repair passes are allowed. One repair pass is one AI call for the complete set of current `FIX` issues; it returns replacements keyed by `paragraph_id`. Unaffected paragraphs are supplied only as context and must not be changed. A second pass runs only when a check of repaired fragments still reports a material `FIX` or the first repair introduced one. It is not used for style polishing. The normal article remains intact; unaffected paragraphs are never regenerated. A full regeneration is allowed once only for `systemic_problem=true`, such as a wrong city/day/story, a fabricated central event, or a structurally unsalvageable article.

After two repair passes, any remaining local `FIX` is removed or replaced with the most conservative wording supported by the relevant Story Card. An unsafe high-risk fragment is excluded. If the unresolved fragment is the headline, lead or central story, the system may perform the one systemic regeneration; if that is not safe or successful, it uses the thematic fallback instead of suppressing the whole release.

If the fact checker times out or fails, a structurally valid writer output is published and the failure is logged. A cheap deterministic preflight checks non-empty content, headline presence, valid Markdown, absence of raw JSON and absence of internal `P###`/`S######` markers. It is not a replacement factual validator.

## 6. Deterministic fallback and publication guarantee

If editorial analysis fails after provider cascade and any context-specific batching, build Story Cards locally from cleaned messages. Existing short digest output may contribute priority signals when available, but fallback does not depend on it and it cannot introduce facts absent from the messages.

The fallback uses conservative topic classification, normalized text similarity, district/topic/reply context, time proximity and repetition. It must not infer causality when clustering. For example, “слышали взрыв” and “на Колонии нет света” may share a broad daily block but cannot become “после взрыва отключился свет” without an explicit source.

Fallback cards retain a compact form:

```json
{
  "topic": "Водоснабжение",
  "news_or_official_facts": [],
  "community_observations": [],
  "useful_details": [],
  "source_refs": []
}
```

The renderer normally creates 3–8 thematic blocks, fewer or more only when the content warrants it. Weak or empty topics are omitted. News/official facts are separated from community observations; disagreements are reported side by side rather than resolved algorithmically. The output is a readable thematic digest with a headline and H2 blocks, never the latest raw messages.

The article is expected to be publishable through the main or degraded editorial path whenever substantive local material remains after cleaning. It may be skipped only when no substantive local information remains or a technical failure prevents even a safe thematic result. Failure of editorial analysis or fact checking alone is not sufficient reason to skip publication. The fallback never affects the morning digest.

In dry-run/debug mode, the pipeline may persist diagnostic artifacts under the configured article fallback directory: prepared editorial input, Story Cards, writer draft, fact-check JSON, repair result and final article. These artifacts are optional in normal production runs and must not be required for publication.

## 7. Provider, logging and compatibility behavior

The existing provider cascade remains responsible for quota, rate-limit, timeout, authentication and model failures. Editorial stages use the same provider abstraction. Provider errors do not expose API keys and do not silently publish raw input.

Useful operational logs include:

```text
Collected 2234 messages
Prepared 1980 editorial candidates
Editorial analysis found 8 stories
Drafting daily article
Light fact check: 1 fix, 2 warnings
Applied 1 targeted repair pass
Article ready
```

Degraded logs include the reason for the transition:

```text
Editorial analysis unavailable after provider cascade: timeout
Using deterministic editorial fallback
```

The existing morning digest, Telegram collection, Telegraph publication and Instant View delivery remain separate concerns. Existing provider fallback, retry, 5000-message safety ceiling and backwards-compatible configuration behavior are preserved.

## 8. Test plan

Replace tests that enforce the obsolete claim-registry publication gate with tests for the new editorial contract. Preserve useful safety invariants: no invented numbers, mechanisms, official actions or high-risk responsibility claims.

Required coverage:

1. Optional channel/topic `source_type`, default `mixed`, invalid values, and role precedence.
2. Backwards-compatible `Message` metadata plus collector extraction of IDs and reply/topic context.
3. Full-bundle editorial analysis as the default; batching only for explicit context-size errors.
4. Story Card parsing, element-level refs, editorial angle, timeframe/status fields and mixed message labels.
5. Community synthesis accepted; unsupported technical mechanisms, sales trends, causes and high-risk claims flagged.
6. `PASS/WARN/FIX` normalization, paragraph IDs, targeted repair, two-pass maximum and preservation of unaffected paragraphs.
7. Fact-checker failure publishes structurally valid writer output.
8. Conservative deterministic clustering, no inferred causality, contradiction preservation and no raw-message fallback.
9. Advertising/currency/spam exclusion.
10. A realistic integration fixture with news, 30–50 community messages, replies, ads, contradictions, several districts and one high-risk topic; verify a coherent article rather than a Story Card list.
11. Article availability through the main path or thematic fallback when substantive material exists.

## Implementation boundary

Implementation should be limited to the article pipeline, source-role configuration, compatible message metadata, `SKILL.md`, and related tests/docs. The existing morning digest and unrelated provider/storage features are not to be redesigned as part of this change.
