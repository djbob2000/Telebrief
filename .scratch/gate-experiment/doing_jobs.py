"""Read-only: the Procrastinate jobs currently in 'doing' and their last event (scratch)."""
import os
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as c:
    rows = c.execute(
        """
        SELECT j.id, j.task_name, j.queue_name, j.attempts, j.scheduled_at,
               (SELECT max(e.at) FROM procrastinate.procrastinate_events e WHERE e.job_id = j.id) AS last_event,
               now()
        FROM procrastinate.procrastinate_jobs j
        WHERE j.status = 'doing'
        ORDER BY j.id
        """
    ).fetchall()
    for r in rows:
        print(r)
    print("todo by task:", c.execute(
        "SELECT task_name, count(*) FROM procrastinate.procrastinate_jobs WHERE status='todo' GROUP BY 1"
    ).fetchall())
