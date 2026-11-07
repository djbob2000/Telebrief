\pset pager off
\timing on
SELECT count(*) AS active_rows FROM story_cluster_state sc JOIN stories s ON s.id=sc.story_id WHERE s.lifecycle_state IN ('candidate','active','reopened') AND sc.last_seen_at >= now() - interval '72 hours';
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)
SELECT sc.story_id, 1.0 - (sc.centroid <=> (SELECT centroid FROM story_cluster_state ORDER BY story_id DESC LIMIT 1)) AS similarity
FROM story_cluster_state sc JOIN stories s ON s.id = sc.story_id
WHERE s.edition_id = 1 AND s.lifecycle_state IN ('candidate','active','reopened')
  AND sc.model = (SELECT model FROM story_cluster_state ORDER BY story_id DESC LIMIT 1)
  AND sc.dimensions = 1536 AND sc.last_seen_at >= now() - interval '72 hours'
ORDER BY sc.centroid <=> (SELECT centroid FROM story_cluster_state ORDER BY story_id DESC LIMIT 1) ASC LIMIT 20;
