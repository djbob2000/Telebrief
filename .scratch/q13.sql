\pset pager off
SELECT 'succeeded_last_hour', count(*), round(avg(extract(epoch from (completed_at - started_at)))::numeric,1) AS avg_sec, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from (completed_at - started_at)))::numeric,1) AS p50_sec FROM event_revision_processing_state WHERE status='succeeded' AND completed_at > now() - interval '1 hour';
SELECT 'fragments_last_hour', count(*) FROM source_fragments WHERE created_at > now() - interval '1 hour';
SELECT 'emb_last_hour', count(*) FROM fragment_embedding_vectors WHERE created_at > now() - interval '1 hour';
