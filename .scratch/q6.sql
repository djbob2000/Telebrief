\pset pager off
SELECT queue_name, task_name, status, count(*) FROM procrastinate.procrastinate_jobs WHERE status IN ('todo','doing','failed') GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 15;
SELECT count(*) AS workers FROM procrastinate.procrastinate_workers;
