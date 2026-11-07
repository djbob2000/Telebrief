"""Read-only: metadata of one publication run, model-like strings only (scratch)."""
import json
import os
import re
import sys
import psycopg

run_id = int(sys.argv[1])
with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    meta, writer = c.execute(
        "SELECT metadata, writer_policy_id FROM publication_runs WHERE id = %s", (run_id,)
    ).fetchone()
    print("writer_policy_id:", writer)
    text = json.dumps(meta, ensure_ascii=False)
    found = sorted(set(re.findall(r'"([a-z0-9.\-]+/[A-Za-z0-9.\-:_]+)"', text)))
    print("model-like strings:", found[:30])
    print("metadata keys:", list(meta.keys())[:40] if isinstance(meta, dict) else type(meta))
