\pset pager off
\timing on
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)
SELECT sc.story_id, sc.updated_at, sc.fragment_count, sc.last_seen_at
FROM story_cluster_state sc JOIN stories s ON s.id = sc.story_id
WHERE s.edition_id = 1 AND s.lifecycle_state IN ('candidate','active','reopened')
  AND sc.model = (SELECT model FROM story_cluster_state ORDER BY story_id DESC LIMIT 1)
  AND sc.dimensions = 1536 AND sc.last_seen_at >= now() - interval '72 hours';
SELECT count(*) FROM story_cluster_state sc JOIN stories s ON s.id=sc.story_id WHERE s.edition_id=1 AND s.lifecycle_state IN ('candidate','active','reopened') AND sc.last_seen_at >= now() - interval '72 hours';
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)
SELECT story_id, updated_at, centroid FROM story_cluster_state WHERE story_id = ANY(ARRAY(SELECT story_id FROM story_cluster_state ORDER BY last_seen_at DESC LIMIT 20));
