"""Output detailed pairwise merge predicate evaluation for 6 electricity stories."""

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
    async with infra.uow.transaction() as conn:
        cur = await conn.execute('SELECT pr.id FROM publications p JOIN publication_runs pr ON pr.id = p.publication_run_id ORDER BY p.created_at DESC LIMIT 1')
        run_id = (await cur.fetchone())[0]
        cur2 = await conn.execute('SELECT id, metadata FROM publication_generation_attempts WHERE metadata::text ILIKE \'%assignments%\' ORDER BY started_at DESC LIMIT 1')
        assignments = (await cur2.fetchone())[1].get('assignments', [])
        assign_map = {a['story_id']: a['rubric_id'] for a in assignments}

        adapter = EventEditorialAdapter(uow=infra.uow, repo=PublicationRepository())
        frozen = await adapter.adapt_inputs_on(conn, run_id)
        cards_by_id = {
            c.id: replace(c, rubric_id=assign_map.get(c.id, ''))
            for c in frozen.analysis.cards
        }

        target_ids = [
            "story:1305",  # Второй месяц без электричества
            "story:1307",  # Точки возле горисполкома / зарядка
            "story:1385",  # Вырубили 1-го числа в 2 ночи
            "story:826",   # Перебои воды и света с ударами дронов
            "story:1231",  # Угроза перебоев с электричеством и газом зимой
            "story:815",   # Света и воды нет
        ]

        batch_stop_tags = _compute_batch_frequent_tags(list(cards_by_id.values()))

        print("TARGET STORIES:")
        for sid in target_ids:
            c = cards_by_id[sid]
            fams = _card_service_families(c)
            tags = _card_specific_tags(c, batch_stop_tags)
            areas = _card_areas(c)
            print(f"{sid}: {c.topic}")
            print(f"   Raw tags: {c.tags}")
            print(f"   Specific tags: {tags}")
            print(f"   Service families: {set(fams)}")
            print(f"   Areas: {areas}")
            print()

        print("=" * 80)
        print("PAIRWISE EVALUATION MATRIX:")
        print("=" * 80)
        for i in range(len(target_ids)):
            for j in range(i + 1, len(target_ids)):
                s_a = target_ids[i]
                s_b = target_ids[j]
                c_a = cards_by_id[s_a]
                c_b = cards_by_id[s_b]

                fams_a = _card_service_families(c_a)
                fams_b = _card_service_families(c_b)
                is_op_a = bool(fams_a) or getattr(c_a, "story_kind", "") == "operational_status"
                is_op_b = bool(fams_b) or getattr(c_b, "story_kind", "") == "operational_status"
                tags_a = _card_specific_tags(c_a, batch_stop_tags)
                tags_b = _card_specific_tags(c_b, batch_stop_tags)
                shared_tags = tags_a & tags_b
                shared_areas = _card_areas(c_a) & _card_areas(c_b)
                shared_lineage = bool(_card_source_lineage(c_a) & _card_source_lineage(c_b))
                compat = _are_cards_merge_compatible(c_a, c_b, batch_stop_tags)

                # Identify predicate failure point
                fail_reasons = []
                if is_op_a or is_op_b:
                    if not (is_op_a and is_op_b):
                        fail_reasons.append("ASYMMETRIC_OPERATIONAL (one is op, other is not)")
                    if len(fams_a) != 1:
                        fail_reasons.append(f"AMBIGUOUS_SERVICE_FAMILY_A: {set(fams_a)} (len != 1)")
                    if len(fams_b) != 1:
                        fail_reasons.append(f"AMBIGUOUS_SERVICE_FAMILY_B: {set(fams_b)} (len != 1)")
                    if fams_a != fams_b:
                        fail_reasons.append(f"SERVICE_FAMILY_MISMATCH: {set(fams_a)} != {set(fams_b)}")
                    if not (shared_tags or shared_areas or shared_lineage):
                        fail_reasons.append("NO_SHARED_EVIDENCE (no shared tags, areas, or lineage)")
                else:
                    if len(shared_tags) < 2 and not shared_lineage:
                        fail_reasons.append(f"NON_OP_INSUFFICIENT_TAGS: shared={len(shared_tags)} < 2")

                outcome = "✅ MERGE COMPATIBLE" if compat else f"❌ REJECTED: {'; '.join(fail_reasons)}"
                print(f"[{s_a}] vs [{s_b}]: {outcome}")
                print(f"    Shared tags: {shared_tags}, Shared areas: {shared_areas}")
                print()

    await infra.close()

if __name__ == "__main__":
    asyncio.run(main())
