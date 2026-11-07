# Group reply chains before Gate?

Type: grilling
Status: resolved
Blocked by: 02, 05

## Question

Should reply chains be grouped into one unit before Gate, as a separate rule, not by changing `join_similarity`? If so, by which rule (reply link, time window, chain-length limit), and how should we avoid gluing different events from the same chat together?

## Answer

**No grouping and no inheritance.** Reasons (see [05](05-reply-chain-measurement.md)):
- the saving is about zero, because each reply still needs its own triage;
- inheriting the parent's decision would drop real reports (80% of replies hang on DROP parents);
- the parent is already passed to triage as non-citable context, so the gain from grouping is small.

Stage 2 of [plan.md](../plan.md) is closed with this decision. The reply-chain idea does not reduce calls.
