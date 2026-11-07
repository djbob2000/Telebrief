\pset pager off
SELECT now();
SELECT e.type, count(*), max(e.at) FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id WHERE j.task_name='process_event_revisions' AND e.at > now() - interval '30 minutes' GROUP BY 1;
SELECT id, status FROM procrastinate.procrastinate_jobs WHERE status='doing' ORDER BY id;
