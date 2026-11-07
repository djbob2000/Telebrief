\pset pager off
SELECT now() AS server_now;
SELECT 'periodic', periodic_id, defer_timestamp FROM procrastinate.procrastinate_periodic_defers WHERE periodic_id = 'requeue-unfinished-revisions' ORDER BY id DESC LIMIT 2;
SELECT 'state', status, count(*) FROM event_revision_processing_state GROUP BY status ORDER BY status;
SELECT 'jobs', task_name, status, count(*) FROM procrastinate.procrastinate_jobs WHERE status IN ('todo','doing') GROUP BY 2,3;
SELECT 'emb_last_15m', count(*) FROM fragment_embedding_vectors WHERE created_at > now() - interval '15 minutes';
SELECT 'stories_last_1h', count(*) FROM stories WHERE created_at > now() - interval '1 hour';
