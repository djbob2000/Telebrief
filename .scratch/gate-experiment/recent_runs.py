"""Read-only: triage run counts per model and status in the last 30 minutes (scratch)."""
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    rows = c.execute(
        """
        SELECT model, status, count(*), max(started_at)
        FROM story_event_triage_runs
        WHERE started_at > now() - interval '30 minutes'
        GROUP BY model, status
        ORDER BY max(started_at) DESC
        """
    ).fetchall()
    for r in rows:
        print(r)
    print("rows:", len(rows))
