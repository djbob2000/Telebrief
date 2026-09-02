"""Trace the 4 exemplars and related anomalies from the latest digest run."""

from __future__ import annotations

import asyncio

import psycopg
from psycopg.rows import dict_row


async def main():
    conn = await psycopg.AsyncConnection.connect(
        "postgresql://localhost:5432/telebrief",
        row_factory=dict_row,
    )
    async with conn:
        print("=" * 80)
        print("EXEMPLAR 1: 'Питьевая вода на розлив — 3 ₽/литр' -> City Situation trace")
        print("=" * 80)
        cur = await conn.execute(
            """
            SELECT sr.story_id, sr.revision_no, sr.title, sr.summary, sr.event_payload,
                   td.decision as triage_decision, td.retention, td.enrichment, td.reason as triage_reason
            FROM story_revisions sr
            LEFT JOIN story_event_triage_decisions td ON td.story_id = sr.story_id
            WHERE sr.event_payload::text ILIKE '%3 ₽%' OR sr.event_payload::text ILIKE '%3 руб%'
               OR sr.title ILIKE '%3 ₽%' OR sr.summary ILIKE '%3 ₽%'
            ORDER BY sr.created_at DESC LIMIT 3
            """
        )
        for r in await cur.fetchall():
            print(f"Story ID: {r['story_id']}")
            print(f"Title: {r['title']}")
            print(f"Summary: {r['summary']}")
            print(
                f"Triage Decision: {r['triage_decision']}, Retention: {r['retention']}, Enrichment: {r['enrichment']}"
            )
            print(f"Triage Reason: {r['triage_reason']}")
            payload = r["event_payload"] or {}
            print(f"Topic: {payload.get('topic')}")
            print(f"Tags: {payload.get('tags')}")
            print(f"Evidence items count: {len(payload.get('evidence_items', []))}")
            for evi in payload.get("evidence_items", []):
                if "3" in str(evi) or "вод" in str(evi).lower():
                    print(f"  - Kind: {evi.get('kind')}, PubUse: {evi.get('publication_use')}")
                    print(f"    Text: {evi.get('text')}")
                    print(f"    Service state: {evi.get('service_state')}")

            # Also show fragments of this story
            cur_frags = await conn.execute(
                """
                SELECT sf.id, sf.text_content
                FROM source_fragments sf
                JOIN story_fragments stf ON stf.fragment_id = sf.id
                WHERE stf.story_id = %s
                """,
                (r["story_id"],),
            )
            print("  Story Fragments:")
            for f in await cur_frags.fetchall():
                print(f"    [{f['id']}]: {f['text_content'][:200]}")

        print("\n" + "=" * 80)
        print("EXEMPLAR 2: Classified / commercial noise (потерянные ключи & магазин Ресурс)")
        print("=" * 80)
        cur = await conn.execute(
            """
            SELECT sr.story_id, sr.title, sr.summary, sr.event_payload,
                   td.decision as triage_decision, td.retention, td.enrichment, td.reason as triage_reason,
                   td.exclusion_reason
            FROM story_revisions sr
            LEFT JOIN story_event_triage_decisions td ON td.story_id = sr.story_id
            WHERE sr.event_payload::text ILIKE '%потеряли ключи%'
               OR sr.event_payload::text ILIKE '%магазин Ресурс%'
               OR sr.event_payload::text ILIKE '%Коммунаров, 49%'
            ORDER BY sr.created_at DESC
            """
        )
        for r in await cur.fetchall():
            print(f"\n--- Story {r['story_id']} ---")
            print(f"Title: {r['title']}")
            print(f"Triage: {r['triage_decision']} / {r['retention']} / {r['enrichment']}")
            print(f"Exclusion Reason: {r['exclusion_reason']}")
            print(f"Triage Reason: {r['triage_reason']}")
            payload = r["event_payload"] or {}
            print(f"Topic: {payload.get('topic')}")
            print(f"Tags: {payload.get('tags')}")
            print("Evidence items:")
            for evi in payload.get("evidence_items", []):
                print(
                    f"  - Kind: {evi.get('kind')}, PubUse: {evi.get('publication_use')}: {evi.get('text')[:100]}"
                )

        print("\n" + "=" * 80)
        print("EXEMPLAR 4: Учреждение 8:00–16:00 (потеря названия)")
        print("=" * 80)
        cur = await conn.execute(
            """
            SELECT sr.story_id, sr.title, sr.summary, sr.event_payload
            FROM story_revisions sr
            WHERE sr.event_payload::text ILIKE '%8:00 до 16:00%'
               OR sr.title ILIKE '%8:00 до 16:00%'
            ORDER BY sr.created_at DESC LIMIT 5
            """
        )
        for r in await cur.fetchall():
            print(f"\n--- Story {r['story_id']} ---")
            print(f"Title: {r['title']}")
            print(f"Summary: {r['summary']}")
            payload = r["event_payload"] or {}
            print(f"Topic: {payload.get('topic')}")
            print(f"Tags: {payload.get('tags')}")
            print("Evidence items:")
            for evi in payload.get("evidence_items", []):
                print(
                    f"  - Kind: {evi.get('kind')}, PubUse: {evi.get('publication_use')}: {evi.get('text')}"
                )
            cur_frags = await conn.execute(
                """
                SELECT sf.id, sf.text_content
                FROM source_fragments sf
                JOIN story_fragments stf ON stf.fragment_id = sf.id
                WHERE stf.story_id = %s
                """,
                (r["story_id"],),
            )
            print("  Raw Fragments:")
            for f in await cur_frags.fetchall():
                print(f"    [{f['id']}]: {f['text_content']}")

        print("\n" + "=" * 80)
        print("EXEMPLAR 5 (BONUS): 'Миранда на Морозова и Гайдара' -> почему в 'Другое'?")
        print("=" * 80)
        cur = await conn.execute(
            """
            SELECT sr.story_id, sr.title, sr.summary, sr.event_payload
            FROM story_revisions sr
            WHERE sr.event_payload::text ILIKE '%Морозова и Гайдара%'
            ORDER BY sr.created_at DESC LIMIT 1
            """
        )
        r = await cur.fetchone()
        if r:
            print(f"Story {r['story_id']}: {r['title']}")
            payload = r["event_payload"] or {}
            print(f"Topic: {payload.get('topic')}")
            print(f"Tags: {payload.get('tags')}")
            print(f"Category: {payload.get('category')}")
            print("Evidence items:")
            for evi in payload.get("evidence_items", []):
                print(
                    f"  - Kind: {evi.get('kind')}, PubUse: {evi.get('publication_use')}: {evi.get('text')}"
                )

        print("\n" + "=" * 80)
        print("EXEMPLAR 6 (BONUS): 'В районе Самолёта вода есть' + 'накопительные баки'")
        print("=" * 80)
        cur = await conn.execute(
            """
            SELECT sr.story_id, sr.title, sr.summary, sr.event_payload
            FROM story_revisions sr
            WHERE sr.event_payload::text ILIKE '%Район самолёта%'
               OR sr.event_payload::text ILIKE '%накопительных бак%'
            ORDER BY sr.created_at DESC LIMIT 3
            """
        )
        for r in await cur.fetchall():
            print(f"\nStory {r['story_id']}: {r['title']}")
            payload = r["event_payload"] or {}
            print(f"Topic: {payload.get('topic')}")
            print(f"Tags: {payload.get('tags')}")
            print("Evidence items:")
            for evi in payload.get("evidence_items", []):
                print(
                    f"  - Kind: {evi.get('kind')}, PubUse: {evi.get('publication_use')}: {evi.get('text')}"
                )


if __name__ == "__main__":
    asyncio.run(main())
