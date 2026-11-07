"""Read-only: revision processing states and job queue counts (scratch)."""
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    print("revisions:", c.execute("SELECT status, count(*) FROM event_revision_processing_state GROUP BY status ORDER BY status").fetchall())
    print("last triage run:", c.execute("SELECT model, status, started_at FROM story_event_triage_runs ORDER BY started_at DESC LIMIT 1").fetchall())
