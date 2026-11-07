# Batch size and batch filling

Type: grilling
Status: resolved
Blocked by: 01 (resolved)

## Question

What size should a triage batch be? Today it is 10 stories, with an average of 7.7 and about 14k characters against the 48k limit. Should we add a fill debounce so batches go out full? Balance the cost of failure (blast radius) against fewer calls.

## Answer (superseded by the correction below)

Measured on the database, 7 days up to 2026-10-18 (read-only):

| Batch size (stories per call) | Runs | Failed share |
|---|---|---|
| 1 | 786 | 61% |
| 2–4 | 5 | 0% |
| 5–7 | 3 | 33% |
| 8–9 | 4 | 25% |
| 10 | 1,762 | 26% |

Two findings:
1. **The failure rate does not grow with batch size.** Full batches of 10 fail less often (26%) than single-story runs (61%). Failures come from specific stories, not from crowding a batch.
2. **The real problem is underfilled batches.** 11,747 stories were decided in 7 days in 2,657 calls, so an average call carries about 4.4 stories against a limit of 10. Batches are formed only from the revisions in the current processing task (`process_event_revisions_task` → `eligible[...]` in `src/jobs/event_processing.py`). Each ingestion run enqueues its own task, so small arrivals produce small batches.

Upper bound: if every call were full (10 stories), calls would fall to about 1,175 a week, or −56%. The realistic gain will be smaller, because waiting adds latency and some stories are triaged in any case.

Decision:
- **Do not raise `triage_batch_size` to 20 yet.** The output size of a 10-story batch is already about 6k characters, and a 20-story batch has not been tested with the real model. Testing costs production budget, so it waits for a decision.
- **Fill, not size.** Wait for the batch to fill (up to about 90 s) before dispatching. This is the bigger gain, and it is what this ticket should implement.
- **Not implemented in this session.** Waiting means re-enqueueing a task that already holds revision claims with leases. That is a change to processing state, so it needs integration tests against PostgreSQL first. Next step: design the requeue in `process_event_revisions_task` and write those tests.

## Correction (same day): the batches are full; the waste is singleton recovery

The fill hypothesis above is **wrong**. Checked against the code path that is actually live.

The live path is `process_background_authority_batch` (`src/jobs/event_authority.py`), and it calls `EventAuthorityService.process_batch` (`src/processing/event_authority.py`). After a batch of 10, every story reported as `missing` or `invalid` is sent **alone** to the model, up to `triage_split_max_extra_calls_per_cycle: 8` extra calls per cycle.

Measured over 7 days (2,560 calls):
- batches of 10: 1,762 runs, of which 1,303 succeeded and 459 failed;
- one-story runs that start within 60 s after a batch: **690** (27% of all calls); **422 failed, 268 succeeded**.

So the calls are not underfilled. Singleton recovery costs about 27% of all triage calls and yields about 268 decisions a week, roughly 2.3% of decisions.

The reason the singletons fail (39% success) is not measured yet. The logs do not carry the original reason for each recovered story. The pure-fill plan below is withdrawn.

Next: ticket [09](09-singleton-recovery-policy.md).
