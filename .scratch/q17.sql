\pset pager off
SELECT job_id, type, at FROM procrastinate.procrastinate_events WHERE job_id IN (282463, 282464, 282459, 282461) ORDER BY job_id, at;
SELECT 'started_recent', job_id, at FROM procrastinate.procrastinate_events WHERE type='started' AND at > now() - interval '40 minutes' ORDER BY at;
