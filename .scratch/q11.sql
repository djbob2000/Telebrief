\pset pager off
SELECT now() AS server_now;
SELECT 'failed', count(*) FROM event_revision_processing_state WHERE status = 'failed';
SELECT 'pending', count(*) FROM event_revision_processing_state WHERE status = 'pending';
SELECT 'running', count(*) FROM event_revision_processing_state WHERE status = 'running';
SELECT 'queued_proc', count(*) FROM procrastinate.procrastinate_jobs WHERE status IN ('todo','doing') AND task_name = 'process_event_revisions';
