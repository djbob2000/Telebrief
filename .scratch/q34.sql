\pset pager off
SELECT now();
SELECT status, count(*), min(updated_at), max(updated_at), min(claim_expires_at), max(claim_expires_at), count(DISTINCT claim_token) FROM event_revision_processing_state WHERE status='running' GROUP BY 1;
SELECT j.id, j.status, to_char(s.at,'HH24:MI:SS') AS started, to_char(f.at,'HH24:MI:SS') AS ended, round(extract(epoch from f.at - s.at)) AS secs
FROM procrastinate.procrastinate_jobs j
LEFT JOIN LATERAL (SELECT at FROM procrastinate.procrastinate_events e WHERE e.job_id=j.id AND e.type='started' ORDER BY at LIMIT 1) s ON true
LEFT JOIN LATERAL (SELECT at FROM procrastinate.procrastinate_events e WHERE e.job_id=j.id AND e.type IN ('succeeded','failed') ORDER BY at DESC LIMIT 1) f ON true
WHERE j.task_name='process_event_revisions' AND s.at > now() - interval '2 hours'
ORDER BY s.at DESC LIMIT 15;
