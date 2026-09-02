"""Analyze why electricity stories in infrastructure rubric did not merge."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.digest_presentation import (
    _are_cards_merge_compatible,
    _card_areas,
    _card_service_families,
    _card_source_lineage,
    _card_specific_tags,
    _compute_batch_frequent_tags,
)
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.repository import PublicationRepository


async def main():
    config = load_config()
    infra = await build_infrastructure(config.database)
    uow = infra.uow
    repo = PublicationRepository()

    async with uow.transaction() as conn:
        cur = await conn.execute(
            """
            SELECT pr.id as run_id
            FROM publications p
            JOIN publication_runs pr ON pr.id = p.publication_run_id
            ORDER BY p.created_at DESC LIMIT 1
            """
        )
        row = await cur.fetchone()
        run_id = row[0]

        cur2 = await conn.execute(
            """
            SELECT id, metadata
            FROM publication_generation_attempts
            WHERE metadata::text ILIKE '%assignments%'
            ORDER BY started_at DESC LIMIT 1
            """
        )
        att_row = await cur2.fetchone()
        assignments = att_row[1].get("assignments", [])
        assign_map = {a["story_id"]: a["rubric_id"] for a in assignments}

        adapter = EventEditorialAdapter(uow=uow, repo=repo)
        frozen = await adapter.adapt_inputs_on(conn, run_id)
        cards = [replace(c, rubric_id=assign_map.get(c.id, "")) for c in frozen.analysis.cards]
        print(f"Loaded {len(cards)} classified story cards")

        # Filter infrastructure cards
        infra_cards = [c for c in cards if c.rubric_id == "infrastructure"]
        print(f"Infrastructure cards count: {len(infra_cards)}")

        batch_stop_tags = _compute_batch_frequent_tags(cards)
        print("Batch stop tags count:", len(batch_stop_tags))
        print("Batch stop tags:", batch_stop_tags)

        # Look at the electricity-related cards in infrastructure
        elec_cards = []
        for c in infra_cards:
            fams = _card_service_families(c)
            text = f"{c.topic} {c.summary} {' '.join(c.tags)}"
            if "электр" in text.lower() or "свет" in text.lower() or "electricity" in fams:
                elec_cards.append(c)

        print(f"\nFound {len(elec_cards)} electricity-related cards in infrastructure:")
        for idx, c in enumerate(elec_cards[:15]):
            fams = _card_service_families(c)
            tags = _card_specific_tags(c, batch_stop_tags)
            areas = _card_areas(c)
            print(f"\n[{idx}] Story {c.id}: {c.topic}")
            print(f"    Tags (raw): {c.tags}")
            print(f"    Tags (specific): {tags}")
            print(f"    Service families: {set(fams)}")
            print(f"    Areas: {areas}")

        # Pairwise compatibility analysis between the first 8 electricity cards!
        print("\n" + "=" * 60)
        print("PAIRWISE MERGE COMPATIBILITY ANALYSIS:")
        print("=" * 60)
        for i in range(min(8, len(elec_cards))):
            for j in range(i + 1, min(8, len(elec_cards))):
                c_a = elec_cards[i]
                c_b = elec_cards[j]
                compat = _are_cards_merge_compatible(c_a, c_b, batch_stop_tags)
                fams_a = _card_service_families(c_a)
                fams_b = _card_service_families(c_b)
                tags_a = _card_specific_tags(c_a, batch_stop_tags)
                tags_b = _card_specific_tags(c_b, batch_stop_tags)
                shared_tags = tags_a & tags_b
                shared_areas = _card_areas(c_a) & _card_areas(c_b)
                lineage_a = _card_source_lineage(c_a)
                lineage_b = _card_source_lineage(c_b)
                shared_lineage = bool(lineage_a & lineage_b)

                # Check why it failed if compat is False:
                reason = "OK"
                is_op_a = bool(fams_a) or getattr(c_a, "story_kind", "") == "operational_status"
                is_op_b = bool(fams_b) or getattr(c_b, "story_kind", "") == "operational_status"
                if not compat:
                    if is_op_a or is_op_b:
                        if not (is_op_a and is_op_b):
                            reason = (
                                f"one is op, other is not: is_op_a={is_op_a}, is_op_b={is_op_b}"
                            )
                        elif len(fams_a) != 1 or len(fams_b) != 1:
                            reason = f"family len != 1: fams_a={set(fams_a)}, fams_b={set(fams_b)}"
                        elif fams_a != fams_b:
                            reason = f"family mismatch: {set(fams_a)} != {set(fams_b)}"
                        elif not (shared_tags or shared_areas or shared_lineage):
                            reason = "no shared specific tags, areas, or lineage"
                    else:
                        reason = f"non-op: shared_specific_count={len(shared_tags)} < 2 and no shared lineage"

                print(f"({i} vs {j}) {c_a.id} vs {c_b.id} -> COMPAT: {compat} | Reason: {reason}")
                print(f"    Tags A: {tags_a}")
                print(f"    Tags B: {tags_b}")

    await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
