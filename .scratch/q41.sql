SELECT task_name, status, count(*) FROM procrastinate.procrastinate_jobs WHERE status IN ('doing','todo') GROUP BY 1,2;
SELECT state, wait_event_type, round(extract(epoch from now()-xact_start)) FROM pg_stat_activity WHERE datname IS NOT NULL AND xact_start IS NOT NULL;
