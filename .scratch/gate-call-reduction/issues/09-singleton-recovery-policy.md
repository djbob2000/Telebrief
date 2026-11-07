# Is singleton recovery worth 27% of triage calls?

Status: in progress (instrumented, awaiting one week of logs)
Type: task (AFK) then grilling
Blocked by: —

## Question

After a batch of 10 has invalid or missing stories, `EventAuthorityService.process_batch` sends each of them alone to the model (`src/processing/event_authority.py`, the `recoverable_targets` loop, budget `triage_split_max_extra_calls_per_cycle: 8`). In 7 days this cost 690 calls (27% of all triage calls) and recovered 268 stories (39%).

Decide which recovered stories are worth a call:
- Does a singleton succeed mostly for `missing` stories (output dropped a story, a truncation artifact) or for `invalid` ones?
- Are the invalid ones mostly the deterministic refusals (`unsupported_publish_extraction`, see [01](01-triage-invalid-response-root-cause.md))? A singleton call repeats the same refusal in most cases.

## Measurement needed first

Per recovered story, record the original reason (`missing` or the invalid reason from the batch) and the singleton outcome. The batch result does not carry reasons today, so this needs a small plumbing change plus a log event, then one week of data.

## Options (for the grilling)

1. Recovery only for `missing` stories, not for deterministic refusals.
2. Lower the per-cycle budget from 8 to 2 or 3.
3. Keep as is.

No production change until the breakdown is in.

## Progress

Instrumentation done, no behaviour change:
- `StoryGateBatchResult.invalid_reasons` (`src/processing/event_triage.py`) carries the validation reason for each invalid story.
- `singleton_recovery_fields` (`src/processing/event_authority.py`) builds one INFO log event `authority_singleton_recovery` per story re-sent alone. Fields: `story_id`, `original_reason` (`missing`, a validation reason, or `unknown`), `batch_error_kind`, `singleton_recovered`, `singleton_error_kind`.
- Tests: `tests/test_singleton_recovery_fields.py` (4) and one assertion in `tests/test_gate_extraction_recovery.py`. Full suite: 547 passed. mypy clean on both modules.

Not yet committed or deployed. After a deploy, collect one week and count, per `original_reason`, how many stories were recovered. For example:

    docker compose logs --since 168h telebrief-authority-worker | grep authority_singleton_recovery

Then decide between the options above.
