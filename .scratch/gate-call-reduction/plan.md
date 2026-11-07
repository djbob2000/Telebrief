# Plan: fewer Gate V2 calls, without losing news

Goal (from [map.md](map.md)): ≥60% fewer triage calls, zero loss of useful short reports, and measurement on frozen data.

Order of work: measure first, then take each saving only after a shadow check. Stages 0, 2 and 3 were checked on data and rejected or closed. Each stage is its own commit, deploy and day of observation.

## Stage 0. Invalid responses (investigated, no code change)
Decision: [01](issues/01-triage-invalid-response-root-cause.md).
- Valid decisions from a failed batch are kept. The waste is only the invalid stories, not the call.
- The defer rule for rejected service claims is intentional (`test_failed_service_extraction_defers_instead_of_dropping_as_noise`, AGENTS §0.9). Retries recover about 7%. No rescue, no requeue, no version bump.
- Expected saving: about 0. This stage is closed.

## Stage 1. Measuring instrument (in progress)
Ticket: [02](issues/02-no-loss-metric-and-frozen-set.md). Status: cost report done (`scripts/gate_cost_report.py`, tests in `tests/test_gate_cost_report.py`, run on prod read-only). Frozen set still to do.
- Done: `scripts/gate_cost_report.py`, per day:
  - triage calls, succeeded/partial/failed, singletons;
  - retries, exhausted;
  - tokens/cost (`input_chars`/`output_chars` as a proxy);
  - KEEP/DROP shares.
- Frozen set `gate_eval_<date>`: 7 days of stories with fragments, embeddings and the current Gate decisions. Stored outside git (`data/eval/`), with the export script in the repo.
- The "no loss" metric:
  - **recall on KEEP = 100%**, and **recall on protected DROP = 100%**. Protected means DROP stories with service vocabulary (power/water/gas/heat/connectivity/transport) or a local place. They must reach the model;
  - a manual audit sample of 100 pre-dropped stories, with an allowed false-drop rate of 0 useful items.
- Do this before any filter.

## Stage 2. Reply chains (closed: measured, no change)
Tickets: [05](issues/05-reply-chain-measurement.md) → [06](issues/06-reply-chain-grouping-decision.md).
- Measured: 51% of single-fragment stories are replies, and 80% of their parents are DROP. A reply must not inherit the parent's decision, because the reply is often the report. Grouping saves about 0. Closed.
- 05: count on the frozen set how many single-fragment stories belong to a reply chain and what share of calls chain grouping would remove.
- (Superseded by the measurement, kept for the record:) 06, my decision in advance:
  - if the gain is under 15%, do not group (risk to clustering for little benefit);
  - if over 15%, group in Gate, not in clustering: put the stories of one chain into the same batch, and send a story already marked DROP that only gained new chain replies without a service signal through the re-triage rule (stage 5).
  - `join_similarity` is not touched.

## Stage 3. Embedding pre-filter (closed: measured, about 7%, not pursued)
Tickets: [03](issues/03-embedding-prefilter-feasibility.md), [04](issues/04-prefilter-policy.md).
- kNN vote: loses KEEP stories at any useful saving. Rejected.
- Max-similarity rule (no service marker, t = 0.95): 7.1% of stories pre-dropped, 1 borderline KEEP lost. Not worth the risk.
- Expected saving revised from 50% to about 7%. The main levers are now stage 4 (batch size) and the rest of the tail.
Tickets: [03](issues/03-embedding-prefilter-feasibility.md) → [04](issues/04-prefilter-policy.md).
- 03: offline kNN (k≈15, cosine) on stored embeddings against stored decisions, by time: train on days 1–5, test on days 6–7. Find the threshold at which recall on KEEP plus protected items is 100%, and count the share of calls removed.
- 04, policy (my decision):
  - pre-drop only with kNN confidence ≥ the threshold from 03;
  - **and** no service markers (the same `_detect_service_families` from `operational_semantics.py`);
  - **and** no recognised place from the city profile;
  - **and** no question about a service.
  - Pre-dropped items get a decision `DROP / prefilter_knn` with version `PREFILTER_VERSION`, are stored like ordinary decisions, and are visible in `gate_cost_report`.
- Rollout:
  - **shadow mode** for 5–7 days: log "would drop", but still call the model; compare;
  - then switch on with the `triage_prefilter_enabled` flag;
  - daily audit sample of 20 pre-dropped items in the debug artifact.
- Expected: of the 1,151 conversational and 301 commercial DROPs a day, the large confident part goes without a call.

## Stage 4. Singleton recovery (replaces "fill batches")
Ticket: [07](issues/07-batch-size-and-fill.md) (corrected), [09](issues/09-singleton-recovery-policy.md) (open).
- The batches are full: 1,303 of 1,762 batches of 10 succeeded. The "fill" idea is withdrawn.
- The real cost is singleton recovery: 690 calls a week (27%), 268 recovered stories (39% success).
- Instrumented (no behaviour change): one log event `authority_singleton_recovery` per singleton, with the original reason and the outcome. Needs a deploy to collect a week of data. Then decide between `missing`-only recovery, a smaller budget, or keep.


## Stage 5. Re-triage
Ticket: [08](issues/08-retriage-policy.md). Decision: when a new fragment arrives at a story already marked DROP, re-triage happens only when the new fragment has a service signal, a place, or a question about a service (the same deterministic guards as stage 3), or when the pre-filter does not confirm DROP. Otherwise the decision is extended to the new fragment without a call. Expected: −5…10%.

## Not now (fog)
Per-source priors, a cascade with a cheap model: return to these after stage 3, once real data shows what remains.

## How we check each stage
- Unit tests plus the full `pytest`; ruff and mypy on the touched files.
- `gate_cost_report` 24 h before and after the deploy.
- Recall on the frozen set (stage 1 onwards).
- Look at the morning digest and the evening article for the next day: nothing useful went missing, and the recovered reports are in place.

## Expected total
Stage 0 about 0%, stage 2 about 0%, stage 3 about −7% (measured). Stage 4 (singleton recovery) can save up to 27% of calls, but it recovers 268 stories a week; the trade-off is open (ticket 09). Stage 5 a few percent. Realistic total is about −15…30%, depending on ticket 09, not the earlier −60…70%.
