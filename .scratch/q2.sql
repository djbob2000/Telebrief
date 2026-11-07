\pset pager off
\d collection_runs
SELECT date_trunc('day', started_at) d, count(*) FILTER (WHERE status='success') ok, count(*) FILTER (WHERE status<>'success') bad FROM collection_runs WHERE started_at > now()-interval '14 days' GROUP BY 1 ORDER BY 1;
SELECT count(*) AS procrastinate_todo_or_failed, task_name, status FROM procrastinate_jobs WHERE status IN ('todo','failed','doing') GROUP BY 2,3 ORDER BY 1 DESC LIMIT 20;
SELECT date_trunc('day', created_at) d, status, count(*) FROM story_event_triage_runs WHERE created_at > now()-interval '14 days' GROUP BY 1,2 ORDER BY 1,2;
