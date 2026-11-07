# Issue tracker

This repo uses the **local-markdown** tracker. `gh` is not authenticated here.

## Wayfinding operations

Used by `/wayfinder`. A **map** is a file, and each **ticket** is its own child file.

- **Map**: `.scratch/<effort>/map.md` (Destination / Notes / Decisions so far / Not yet specified / Out of scope).
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line holds the type (`research`/`prototype`/`grilling`/`task`), and a `Status:` line holds `open`/`claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every ticket it lists is `resolved`.
- **Frontier**: the files in `issues/` that are open, unblocked and unclaimed. The lowest number goes first.
- **Claim**: set `Status: claimed` and save before doing any work.
- **Resolve**: append the answer under `## Answer`, set `Status: resolved`, then add a gist plus a link to the map's Decisions so far.

Active maps:
- `.scratch/gate-call-reduction/map.md`: cutting the number of Gate V2 triage calls.
