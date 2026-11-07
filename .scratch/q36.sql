\pset pager off
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze, autovacuum_count FROM pg_stat_user_tables WHERE relname IN ('story_cluster_state','stories','story_fragments','source_fragments','story_fragment_embeddings') ORDER BY relname;
SELECT relname, pg_size_pretty(pg_relation_size(oid)) AS heap, pg_size_pretty(pg_total_relation_size(oid)) AS total FROM pg_class WHERE relname IN ('story_cluster_state','story_cluster_state_pkey') OR oid IN (SELECT reltoastrelid FROM pg_class WHERE relname='story_cluster_state');
SELECT name, setting FROM pg_settings WHERE name IN ('autovacuum','autovacuum_vacuum_scale_factor','autovacuum_vacuum_cost_limit','maintenance_work_mem');
