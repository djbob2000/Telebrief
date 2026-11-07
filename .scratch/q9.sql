\pset pager off
SELECT now() AS server_now;
SELECT id, status FROM procrastinate.procrastinate_jobs WHERE id BETWEEN 282380 AND 282384 ORDER BY id;
SELECT 'runs', source_id, trigger, status, seen_count, new_count, updated_count FROM collection_runs WHERE started_at > now() - interval '30 minutes' ORDER BY started_at DESC LIMIT 8;
SELECT 'proc_jobs', status, count(*) FROM procrastinate.procrastinate_jobs WHERE task_name = 'process_event_revisions' AND id > 282380 GROUP BY status;
SELECT 'emb_last_10m', count(*) FROM fragment_embedding_vectors WHERE created_at > now() - interval '10 minutes';
SELECT 'state', status, count(*) FROM event_revision_processing_state GROUP BY status;
