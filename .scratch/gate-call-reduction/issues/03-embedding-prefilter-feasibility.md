# Can an embedding pre-filter work

Type: task (AFK)
Status: resolved
Blocked by: 02

## Question

Build an offline kNN/centroid classifier on stored fragment embeddings and stored Gate decisions. At what confidence can it predict DROP without losing a single KEEP from the ticket 02 set? What share of triage calls would that remove (for example, gross: 1,151 conversational and 301 commercial out of 1,576)? No new LLM calls.

## Answer

Experiment: 7 days of Gate-labelled fragments (66,729 rows, 9,164 test stories), time split 70/30 (last 2 days are test). Vectors exported read-only from the app container; scripts in `experiments/` (`export_vectors.py`, `knn_local.py`, `knn_maxsim.py`). Labels are the current model's decisions, so a "lost KEEP" means disagreement with today's Gate, not truth.

1. **kNN vote** (share of 15 neighbours labelled DROP): not usable. With the guard "no service marker may be pre-dropped", a 38% pre-drop still loses 52 KEEP stories (6%). Without the guard it loses 96 KEEP and 720 service DROPs. Thresholds above 0.95 change nothing, because the vote saturates at 1.0.
2. **Max similarity** rule: pre-drop if the fragment's max cosine to a training DROP is ≥ t, it is above the max to a training KEEP, and there is no service marker.
   - t = 0.95: 7.1% of stories pre-dropped, 1 KEEP lost (story 55519, an office-hours notice with no location, which Gate itself labelled "reference brief").
   - t = 0.85: 9.3% pre-dropped, 7 KEEP lost.

Conclusion: the best rule saves about 7% of triage calls, not the 50% in [plan.md](../plan.md). That is far below the target, and it still loses one borderline KEEP. A pre-filter does not meet the "no loss" rule with a useful saving. Ticket [04](04-prefilter-policy.md) is therefore closed as **not pursued**.
