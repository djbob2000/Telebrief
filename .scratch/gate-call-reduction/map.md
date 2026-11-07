# Map: cutting Gate V2 triage calls

Label: wayfinder:map

## Destination

A recorded set of decisions (ADR) that cuts Gate V2 triage LLM calls by **≥60%** without losing useful short local reports, plus a reproducible way to measure this on frozen data.

## Notes

- Domain: the Event-First processing layer, in `src/processing/event_triage.py`, `src/jobs/event_processing.py`, `src/processing/fragments.py`, `src/processing/event_clustering.py` and `src/processing/noise_detection.py`.
- Constraints from AGENTS.md §1.1, §2.1 and §2.3:
  - short civic reports must survive;
  - corroboration gates are not allowed;
  - do not touch `join_similarity` to solve presentation problems;
  - any change to triage semantics needs a `TRIAGE_VERSION` bump and a backfill decision.
- The OpenRouter key is shared with production (limit $2 a day). Offline experiments should reuse stored decisions (`story_event_triage_decisions`) and embeddings without new LLM calls.
- Baseline (DB, 24 h up to 2026-10-18):
  - 272 triage calls, 107 of them failed (106 `invalid_response`, 1 timeout); a failed run still keeps the valid decisions of its batch;
  - 0 rich-analysis calls (all 163 KEEP decisions are BRIEF);
  - 1,576 stories, 1,577 DROP decisions (1,151 conversational without `exclusion_reason`, 301 `commercial_classified`, 97 `obvious_noise`);
  - 71% of stories have a single fragment;
  - 149 stories were triaged two or more times;
  - an average batch holds 7.7 stories and about 14k characters, against a 48k limit.
- Skills for decision tickets: grilling and domain-modeling.

## Decisions so far

<!-- one line per closed ticket -->
- [Batch size and fill](issues/07-batch-size-and-fill.md): withdrawn. The batches are full (1,303 of 1,762 succeeded). The 4.4 average came from singleton recovery calls. See 09.
- [Embedding pre-filter feasibility](issues/03-embedding-prefilter-feasibility.md): kNN vote loses KEEP stories. Best max-similarity rule saves about 7% with one borderline KEEP lost. Rejected.
- [Pre-filter policy](issues/04-prefilter-policy.md): not pursued, because of 03.
- [Reply chains: measurement](issues/05-reply-chain-measurement.md): 51% of single-fragment stories are replies, but 80% of their parents are dropped chatter, and the replies are often the reports. Result: no inheritance.
- [Reply chains: grouping decision](issues/06-reply-chain-grouping-decision.md): no grouping, no inheritance. Saving about 0.
- [Why triage fails with invalid_response](issues/01-triage-invalid-response-root-cause.md): deferred stories are mostly deliberate refusals, and the defer rule is kept. Retries recover about 7%. Valid decisions from failed batches are kept, so the waste is small. No code change, no requeue, no version bump.

## Not yet specified

<!-- open questions still to be measured: re-triage (08); what singleton recovery saves against what it recovers (09), the no-loss metric (02) -->

- Source-level priors: chats and news channels have very different DROP rates. Should that change how strict the pre-filter is (spend control only, not proof of locality)?
- A cascade: a cheap model or embeddings before Gate, only for ambiguous items.
- Rollout order of the measures, `TRIAGE_VERSION` bump and backfill of stored decisions.
- Ongoing audit in production of items dropped without the model (sampling, alerts).

## Out of scope

- One LLM call per channel or chat summary: it loses fragment provenance and mixes facts.
- Reducing calls on the publication side (digest/article writers and editors).
