\pset pager off
SELECT date_trunc('day', collected_at) d, count(*) revisions FROM source_item_revisions WHERE collected_at > now()-interval '14 days' GROUP BY 1 ORDER BY 1;
SELECT date_trunc('day', created_at) d, count(*) fragments FROM source_fragments WHERE created_at > now()-interval '14 days' GROUP BY 1 ORDER BY 1;
SELECT date_trunc('day', created_at) d, count(*) emb FROM fragment_embedding_vectors WHERE created_at > now()-interval '14 days' GROUP BY 1 ORDER BY 1;
SELECT date_trunc('day', started_at) d, status, count(*) FROM story_event_triage_runs WHERE started_at > now()-interval '14 days' GROUP BY 1,2 ORDER BY 1,2;
SELECT status, count(*) FROM procrastinate.procrastinate_jobs GROUP BY 1;
SELECT task_name, status, count(*) FROM procrastinate.procrastinate_jobs WHERE status <> 'succeeded' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20;
SELECT max(scheduled_at) FROM procrastinate.procrastinate_jobs;
