\pset pager off
\d procrastinate.procrastinate_events
WITH ev AS (
  SELECT e.job_id, e.type, e.at FROM procrastinate.procrastinate_events e
  JOIN procrastinate.procrastinate_jobs j ON j.id = e.job_id
  WHERE j.task_name = 'process_event_revisions' AND e.at > now() - interval '60 minutes'
)
SELECT 'counts', type, count(*) FROM ev GROUP BY type ORDER BY type;
WITH s AS (SELECT job_id, min(at) FILTER (WHERE type='deferred') AS deferred_at, min(at) FILTER (WHERE type='started') AS started_at, min(at) FILTER (WHERE type IN ('succeeded','failed')) AS ended_at
  FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id
  WHERE j.task_name='process_event_revisions' AND e.at > now() - interval '60 minutes' GROUP BY job_id)
SELECT 'wait_sec_p50', round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from started_at-deferred_at))::numeric,1), 'run_sec_p50', round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from ended_at-started_at))::numeric,1), 'jobs', count(*) FROM s WHERE started_at IS NOT NULL AND ended_at IS NOT NULL;
