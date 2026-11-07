# Why triage fails with invalid_response

Type: task (AFK)
Status: resolved
Blocked by: —

## Question

Why do 39% of triage runs (107 of 272 in 24 h) fail? 56 batches of 10 and 50 single-story runs ended with `invalid_response`. Classify the failures from server logs and debug artifacts: response truncation, invalid JSON, unknown or missing story_ids, schema violations. Decide on recovery: accept the valid part of the response, re-ask only the missing stories, or bisect a batch instead of splitting it into single stories. Code: `src/processing/event_triage.py` (≈1040–1070, 1400–1415, 1588).

## Answer

These are not JSON errors. Server logs of 2026-10-17/18 contain 21 `event_first_gate_invalid_response` events: 20 `unsupported_publish_extraction` and 1 `missing_scope_basis_fragments`. In each event the response parsed, and 8–9 of 10 stories received valid decisions.

The mechanism:
1. The model marks an elliptical chat reply as `service_access / PUBLISH`, for example «АКЗ нет четыре дня» or «Баха между Лермонтова и Жуковского, дали». The service is named only in the parent message.
2. `normalize_service_state_evidence` (`src/processing/operational_semantics.py`) cannot ground the service family in the reply's own text and demotes the evidence to CONTEXT.
3. `event_triage.py:1356` treats this as `unsupported_publish_extraction` and defers the story.
4. A retry gives the same deterministic result → after `triage_max_attempts_per_assignment: 2` the story is `exhausted` and never gets a decision.

Consequences over 7 days: 2,528 triage runs, 937 of them `failed` (37%), 784 singleton runs. **205 stories were lost** with no decision, among them genuine reports («Пионерская частный сектор 12 дней сидит», «С 5-го числа Частный сектор гора»). Calls are wasted on futile retries, and the stories' content is lost; this contradicts AGENTS §0.1 and §2.1.

## Correction after measurement (7 days, 2026-10-11…18)

My earlier claim that about a third of calls was wasted is **wrong**. Checked in `event_triage.py`: invalid stories are deferred, but the valid decisions from the same batch are kept (`results=tuple(valid_results)`). A failed run still produced 8–9 decisions. The waste is only the share of invalid stories, not the whole call.

Measured on the database:
- 11,631 distinct stories triaged in 7 days (about 1,660 a day);
- 201 stories were ever deferred by triage, and 194 of them exhausted their retries;
- of 205 exhausted retries, only 14 later received a decision (about 7%);
- `scripts/gate_cost_report.py` (read-only) shows 2,657 calls, 961 with `failed` status (36%). The `failed` status counts partial batches too, so this share does not mean lost calls.

Why we do **not** rescue these stories by promoting their text: most exhausted stories are deliberate refusals. The normalizer demotes a service claim that the source text does not support. AGENTS §0.9 forbids promoting chat sarcasm and neighbour complaints to operational facts, for example «Ого бедный Пионерская частный сектор 12 дней сидит, а вокруг всё сверкает». The existing test `test_failed_service_extraction_defers_instead_of_dropping_as_noise` encodes this rule on purpose.

Decision (revised):
- no change to the defer rule and no rescue logic;
- no requeue of the 194 exhausted stories (about 7% recovery; a prod write for little gain);
- no `TRIAGE_VERSION` bump: no cached decision changes, so a bump would re-triage the whole window at a cost of a full day of calls;
- the real savings are elsewhere: pre-filter (ticket 03/04) and batch size (ticket 07).

Decision:
- (a) A deterministic validation rejection is not grounds for retrying the model. Resolve the story from the response already received: an evidence item demoted by the normalizer stays as `community_report`. It stays PUBLISH, with the reply's exact words only, when the reply has its own local anchor (a place, an interval, or a "дали/нет/есть" state on a known street or district). Otherwise it stays CONTEXT, and the story gets a cached decision without a retry.
- (b) A run with valid decisions plus invalid stories is recorded as `partial`, not `failed`, so the metric is honest.
- (c) Exhausted stories from the last 7 days go back to triage once after the fix (a one-off requeue).
- (d) Bisection on `invalid_response` is not added: retries only make sense for provider errors.
