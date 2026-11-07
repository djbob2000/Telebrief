\pset pager off
\d sources
SELECT count(*) AS sources_total FROM sources;
SELECT max(created_at) AS last_source_item, count(*) FILTER (WHERE created_at > now()-interval '7 days') AS items_7d FROM source_items;
SELECT date_trunc('day', started_at) d, status, count(*) FROM collection_runs WHERE started_at > now()-interval '10 days' GROUP BY 1,2 ORDER BY 1,2;
SELECT max(finished_at) AS last_collection_finished FROM collection_runs;
