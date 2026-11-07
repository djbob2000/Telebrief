\pset pager off
SELECT now() AS server_now;
SELECT queue_name, task_name, status, count(*) FROM procrastinate.procrastinate_jobs WHERE status IN ('todo','doing') OR scheduled_at > now() - interval '30 minutes' GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 15;
SELECT count(*) AS emb_rows_last_30m FROM fragment_embedding_vectors WHERE created_at > now() - interval '30 minutes';
SELECT status, count(*) FROM story_event_triage_runs WHERE started_at > now() - interval '30 minutes' GROUP BY 1;
