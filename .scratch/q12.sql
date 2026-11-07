\pset pager off
SELECT now() AS server_now;
SELECT to_char(date_trunc('minute', created_at) - (extract(minute from created_at)::int % 5) * interval '1 minute', 'HH24:MI') AS bucket5, count(*) AS embeddings FROM fragment_embedding_vectors WHERE created_at > now() - interval '40 minutes' GROUP BY 1 ORDER BY 1;
SELECT 'jobs', task_name, status, count(*), min(scheduled_at) FROM procrastinate.procrastinate_jobs WHERE status IN ('todo','doing') GROUP BY 2,3;
SELECT 'proc_doing', id, attempts, scheduled_at FROM procrastinate.procrastinate_jobs WHERE status='doing' AND task_name='process_event_revisions' ORDER BY id LIMIT 5;
SELECT 'claim_expiry_running', count(*) FROM event_revision_processing_state WHERE status='running' AND claim_expires_at > now();
