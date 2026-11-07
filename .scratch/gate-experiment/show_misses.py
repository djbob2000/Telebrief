"""Print KEEP stories that the reference kept and a model dropped (read-only, scratch)."""
import asyncio, json, os, sys
import psycopg

REF = json.load(open("/work/sample.json"))["reference"]
MODEL_FILES = sys.argv[1:]
TEXT_SQL = """
SELECT DISTINCT ON (sf.story_id) sf.story_id, left(f.text_content, 160)
FROM story_fragments sf JOIN source_fragments f ON f.id = sf.fragment_id
WHERE sf.story_id = ANY(%s) ORDER BY sf.story_id, sf.id
"""

async def main():
    async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as conn:
        for path in MODEL_FILES:
            dec = json.load(open(path))["decisions"]
            misses = [int(k) for k, v in REF.items() if v == "KEEP" and dec.get(k) == "DROP"]
            print(f"== {path.split('/')[-1]}: KEEP->DROP {len(misses)}")
            if not misses:
                continue
            cur = await conn.execute(TEXT_SQL, (misses,))
            for sid, text in await cur.fetchall():
                print(f"  #{sid}: {' '.join(str(text).split())}")

asyncio.run(main())
