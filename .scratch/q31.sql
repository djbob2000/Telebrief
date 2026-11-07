\pset pager off
SELECT status, count(*) FROM event_revision_processing_state GROUP BY 1 ORDER BY 1;
SELECT 'recoverable', count(*) FROM event_revision_processing_state s WHERE (s.status IN ('pending','failed') AND s.updated_at <= now() - interval '600 seconds') OR (s.status='running' AND s.claim_expires_at <= now());
SELECT 'running_live', count(*) FROM event_revision_processing_state WHERE status='running' AND claim_expires_at > now();
SELECT 'succeeded_last_15m', count(*) FROM event_revision_processing_state WHERE status='succeeded' AND completed_at > now() - interval '15 minutes';
SELECT 'failed_last_15m', count(*) FROM event_revision_processing_state WHERE status='failed' AND updated_at > now() - interval '15 minutes';
SELECT 'wait', wait_event_type, wait_event, count(*) FROM pg_stat_activity WHERE datname IS NOT NULL GROUP BY 1,2,3;
