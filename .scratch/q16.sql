\pset pager off
SELECT now() AS server_now;
WITH s AS (SELECT job_id, min(at) FILTER (WHERE type='started') AS started_at, min(at) FILTER (WHERE type IN ('succeeded','failed')) AS ended_at, min(at) FILTER (WHERE type='deferred') AS deferred_at
  FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id WHERE j.task_name='process_event_revisions' AND e.at > now() - interval '3 hours' GROUP BY job_id)
SELECT 'run_sec_pctl', count(*) FILTER (WHERE ended_at IS NOT NULL) AS done, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from ended_at-started_at))::numeric) AS p50, round(percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch from ended_at-started_at))::numeric) AS p90, round(max(extract(epoch from ended_at-started_at))::numeric) AS max_s FROM s WHERE started_at IS NOT NULL;
SELECT 'running_now', job_id, round(extract(epoch from now()-started_at)) AS running_for_sec FROM s WHERE started_at IS NOT NULL AND ended_at IS NULL;
SELECT 'failed_recent', count(*) FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id WHERE j.task_name='process_event_revisions' AND e.type IN ('failed','deferred_for_retry') AND e.at > now() - interval '3 hours';
