\pset pager off
SELECT task_name, queue_name, status, count(*), max(id) FROM procrastinate.procrastinate_jobs WHERE task_name LIKE '%requeue%' OR task_name LIKE '%unfinished%' GROUP BY 1,2,3 ORDER BY 5 DESC LIMIT 6;
SELECT id, status, scheduled_at FROM procrastinate.procrastinate_jobs WHERE task_name = 'requeue_unfinished_revisions' ORDER BY id DESC LIMIT 3;
SELECT job_id, type, at FROM procrastinate.procrastinate_events WHERE job_id IN (SELECT id FROM procrastinate.procrastinate_jobs WHERE task_name='requeue_unfinished_revisions' ORDER BY id DESC LIMIT 2) ORDER BY job_id, at;
