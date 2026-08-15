# Berdyansk CityProfile v2 — Streets, Aliases & Provider Confidence

Generated: 2026-08-15

## What changed

- Added a street gazetteer derived primarily from the 25.03.2021 municipal neighborhood-committee boundary decision.
- Preserved address/side/segment rules where the municipal source does not assign an entire street to one area.
- Added the 2016 old→new toponym mapping as historical aliases for resolver use.
- Added explicitly published 2024 occupation-era aliases as local resolver aliases, without treating them as Ukrainian legal names.
- Kept municipal committee geography and colloquial geography separate.
- Upgraded Point, +7Telecom and MirTelecom to high-confidence active-at-last-verification based on local editorial confirmation.
- Kept `editorial_scale_area_set.exhaustive_for_scale: false`; street mapping does not by itself enable majority claims.

## Critical resolver rules

1. Entity identity is `(object_type, canonical_name)`: `улица Шевченко` and `бульвар Шевченко` are different objects and map differently.
2. A street can cross committee boundaries; use house number, side and segment when available.
3. If the address is insufficient, return all candidate areas rather than guessing.
4. Colloquial area labels (e.g. “гора”) are a separate dimension from municipal committee areas.
5. Current-event evidence must still come from today's source corpus.

## Coverage summary

This is a resolver gazetteer derived from neighborhood-boundary documents, not a claim that every legally registered street/toponym in Berdyansk is present. Entries include streets, lanes, passages, prospects, squares, parks and other named address objects.

- Street/toponym entries: 446
- Entries with multiple municipal-area candidates: 19
- Municipal areas represented: 13
- Historical 2016 alias pairs: 70
- Explicit 2024 local/occupation-era aliases: 23

## Main sources

- Municipal neighborhood committee boundaries (25.03.2021): https://brd.gov.ua/documents/158004-pro-vnesennia-zmin-do-risennia-berdianskoyi-miskoyi-radi-vid-01082002-no-18-pro-nadannia-dozvolu-na-stvorennia-komitetiv-m
- 2016 rename list: https://www.brd24.com/news/a-40069.html
- 2024 municipal overview of 69 returned Soviet names: https://brd.gov.ua/news/151538-u-to-berdiansku-povernuli-radianski-nazvi-vulic
- Explicit 2024 alias examples: https://www.inform.zp.ua/uk/2024/06/08/283169_u-berdyansku-povernuly-radyanski-nazvy-vulycz/
- Local geographic context map (secondary): https://berd.ua/map/

## Editorial review still required

The 13 municipal committees are a strong structured geography source, but they remain disabled as a majority denominator.
Several streets appear in more than one committee or have segment-specific rules, which confirms that raw street counts must not be used as a proxy for district counts.
