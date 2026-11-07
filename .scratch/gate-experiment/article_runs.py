"""Read-only: recent publication runs by type and status (scratch)."""
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    cols = c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='publication_runs' ORDER BY ordinal_position"
    ).fetchall()
    print("publication_runs columns:", [r[0] for r in cols])
    rows = c.execute(
        """
        SELECT id, publication_type, status, created_at
        FROM publication_runs
        ORDER BY id DESC LIMIT 15
        """
    ).fetchall()
    for r in rows:
        print(r)
