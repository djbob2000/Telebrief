\pset pager off
SELECT job_id, type, at FROM procrastinate.procrastinate_events WHERE job_id IN (282573,282574) ORDER BY job_id, at;
SELECT 'succeeded_last_hour', count(*), min(at), max(at) FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id WHERE j.task_name='process_event_revisions' AND e.type='succeeded' AND e.at > now() - interval '60 minutes';
SELECT 'failed_last_hour', count(*) FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id WHERE j.task_name='process_event_revisions' AND e.type IN ('failed') AND e.at > now() - interval '60 minutes';
SELECT 'last_started', max(at) FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id WHERE j.task_name='process_event_revisions' AND e.type='started';
