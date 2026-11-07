# How many stories are pieces of reply chains

Type: task (AFK)
Status: resolved
Blocked by: —

## Question

How many single-fragment stories are replies inside a chat thread (a reply chain or a short time window) whose neighbours ended up in other stories? Estimate how many triage calls and stories would go away if a whole chain were triaged as one unit.

## Answer

Measured on the database, 7 days up to 2026-10-18 (read-only query):
- 11,631 distinct stories were triaged;
- 9,709 of them (83%) have a single fragment;
- 4,954 of those single-fragment stories (51%) are replies (their source item has `parent_item_id`);
- 4,774 of the replies have a parent that sits in a different story.

Outcome of the parent's own triage decision for those 4,774 replies:
- parent DROP/NONE: 3,808 (80%);
- parent KEEP/BRIEF: 680;
- parent with both decisions over the week: 170;
- parent without a decision: 114;
- parent KEEP/ANALYZE: 2.

Conclusion: most replies hang on parents that were dropped as chatter, and these replies are often the reports themselves. Example: a question «у вас свет есть?» is dropped, and the reply «АКЗ нет четыре дня» carries the report. So **a reply must not inherit the parent's decision**. The parent is useful only as context, and that already happens (`story_reply_parent_contexts` in `event_triage.py`).

Upper bound of call savings from merging chains: about 43% of stories, but that would be a semantic error, so it is not a savings estimate. The real saving from grouping is about zero: a reply still needs its own triage. See [06](06-reply-chain-grouping-decision.md).
