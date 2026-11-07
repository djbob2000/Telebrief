SELECT status, count(*) FROM procrastinate.procrastinate_jobs WHERE task_name='process_event_revisions' AND id > 282575 GROUP BY 1;
