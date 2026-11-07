\pset pager off
SELECT now();
SELECT 'proc', status, count(*) FROM procrastinate.procrastinate_jobs WHERE task_name='process_event_revisions' AND status IN ('todo','doing') GROUP BY 2;
SELECT 'sess', state, wait_event_type, wait_event, round(extract(epoch from now()-query_start)) AS q_s, left(regexp_replace(query, '\s+', ' ', 'g'), 90) AS q FROM pg_stat_activity WHERE datname IS NOT NULL AND pid <> pg_backend_pid() AND state <> 'idle' ORDER BY query_start LIMIT 10;
SELECT 'fragments_recent', count(*) FROM source_fragments WHERE created_at > now() - interval '30 minutes';
