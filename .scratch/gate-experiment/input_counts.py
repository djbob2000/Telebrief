"""Read-only: sealed input row counts for recent article runs (scratch)."""
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    rows = c.execute(
        """
        SELECT r.id, r.publication_type, r.status,
               (SELECT count(*) FROM publication_inputs i WHERE i.publication_run_id = r.id) AS inputs
        FROM publication_runs r
        WHERE r.publication_type = 'daily_article'
        ORDER BY r.id DESC LIMIT 12
        """
    ).fetchall()
    for r in rows:
        print(r)
