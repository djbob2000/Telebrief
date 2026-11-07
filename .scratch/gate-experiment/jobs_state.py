"""Read-only: Procrastinate job counts per queue and status (scratch)."""
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    tables = c.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_name ILIKE '%procrastinate%' OR table_name ILIKE '%job%'"
    ).fetchall()
    print("tables:", tables)
    for schema, name in tables:
        if name.endswith("jobs"):
            rows = c.execute(
                f'SELECT queue_name, status, count(*) FROM "{schema}"."{name}" GROUP BY 1,2 ORDER BY 1,2'
            ).fetchall()
            print(f"{schema}.{name}:", rows)
