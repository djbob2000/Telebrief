\pset pager off
SELECT now();
SELECT status, count(*) FROM event_revision_processing_state GROUP BY 1 ORDER BY 1;
SELECT 'succeeded_last_15m', count(*) FROM event_revision_processing_state WHERE status='succeeded' AND completed_at > now() - interval '15 minutes';
