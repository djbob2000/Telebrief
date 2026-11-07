\pset pager off
SELECT now() AS server_now;
SELECT 'proc_jobs', status, count(*), min(id), max(id) FROM procrastinate.procrastinate_jobs WHERE task_name='process_event_revisions' AND status IN ('todo','doing') GROUP BY 2;
SELECT 'doing_now', id, task_name FROM procrastinate.procrastinate_jobs WHERE status='doing' ORDER BY id;
SELECT 'sweep_defers', id, defer_timestamp FROM procrastinate.procrastinate_periodic_defers WHERE periodic_id='requeue-unfinished-revisions' ORDER BY id DESC LIMIT 3;
SELECT 'running_claim', min(claim_expires_at), max(claim_expires_at), count(*) FILTER (WHERE claim_expires_at > now()) AS live FROM event_revision_processing_state WHERE status='running';
