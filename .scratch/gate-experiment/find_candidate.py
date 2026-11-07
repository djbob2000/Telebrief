"""Read-only: where the rejected weekly candidate for run 367 is stored (scratch)."""
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    for (table,) in c.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND (table_name ILIKE '%candidate%' OR table_name ILIKE '%generation%' OR table_name ILIKE '%attempt%' OR table_name ILIKE '%artifact%')"
    ).fetchall():
        cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (table,)).fetchall()]
        count = c.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        print(table, count, cols)
