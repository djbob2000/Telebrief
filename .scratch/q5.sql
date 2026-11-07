\pset pager off
SELECT now() AS server_now;
SELECT count(*) AS fragments_without_embedding, min(f.created_at) AS oldest, max(f.created_at) AS newest
FROM source_fragments f
WHERE f.created_at > now() - interval '14 days'
  AND NOT EXISTS (SELECT 1 FROM fragment_embedding_vectors v WHERE v.normalized_hash = f.normalized_hash);
SELECT date_trunc('hour', created_at) h, count(*) FROM fragment_embedding_vectors WHERE created_at > now() - interval '3 hours' GROUP BY 1 ORDER BY 1;
SELECT date_trunc('hour', started_at) h, status, count(*) FROM story_event_triage_runs WHERE started_at > now() - interval '3 hours' GROUP BY 1,2 ORDER BY 1,2;
