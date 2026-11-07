\pset pager off
SELECT id, status, attempts, scheduled_at, left(args::text, 80) AS args FROM procrastinate.procrastinate_jobs WHERE task_name='process_event_revisions' ORDER BY id DESC LIMIT 8;
SELECT e.type, count(*), max(e.at) FROM procrastinate.procrastinate_events e JOIN procrastinate.procrastinate_jobs j ON j.id=e.job_id WHERE j.task_name='process_event_revisions' AND e.at > now() - interval '40 minutes' GROUP BY 1;
