\pset pager off
SELECT now();
SELECT 'proc', status, count(*) FROM procrastinate.procrastinate_jobs WHERE task_name='process_event_revisions' AND status IN ('todo','doing') GROUP BY 2;
SELECT 'state', status, count(*) FROM event_revision_processing_state GROUP BY status ORDER BY status;
SELECT 'io_waits', count(*) FROM pg_stat_activity WHERE wait_event_type='IO';
