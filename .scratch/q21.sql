\pset pager off
SELECT 'rows', count(*), pg_size_pretty(pg_total_relation_size('story_cluster_state')) AS total_size FROM story_cluster_state;
SELECT 'indexes', indexname, indexdef FROM pg_indexes WHERE tablename = 'story_cluster_state';
SELECT 'stories_rows', count(*) FROM stories;
SELECT 'db_shared_buffers', current_setting('shared_buffers'), current_setting('effective_cache_size');
SELECT 'cache_hit', round(sum(blks_hit)::numeric / nullif(sum(blks_hit)+sum(blks_read),0) * 100, 2) AS hit_pct FROM pg_stat_database WHERE datname = current_database();
