\pset pager off
SELECT now() AS server_now;
SELECT id, task_name, status, scheduled_at FROM procrastinate.procrastinate_jobs WHERE id BETWEEN 282380 AND 282384 ORDER BY id;
SELECT 'runs', trigger, status, started_at, seen_count, new_count FROM collection_runs WHERE started_at > now() - interval '20 minutes' ORDER BY started_at DESC LIMIT 6;
SELECT 'would_requeue', count(*) FROM (SELECT source_item_revision_id FROM event_revision_processing_state WHERE (status IN ('pending','failed') AND updated_at <= now() - make_interval(secs => 600)) OR (status = 'running' AND claim_expires_at <= now()) LIMIT 500) s;
SELECT 'state', status, count(*) FROM event_revision_processing_state GROUP BY status;
