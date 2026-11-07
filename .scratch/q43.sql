SELECT status, count(*) FROM event_revision_processing_state GROUP BY 1 ORDER BY 1;
SELECT state, round(extract(epoch from now()-query_start)) s, left(regexp_replace(query,'\s+',' ','g'),60) q FROM pg_stat_activity WHERE datname IS NOT NULL AND state='active' AND pid<>pg_backend_pid();
