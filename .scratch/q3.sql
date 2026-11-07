\pset pager off
SELECT table_name, string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name IN ('source_item_revisions','source_fragments','fragment_embedding_vectors','story_event_triage_runs','stories') GROUP BY 1;
SELECT table_schema, table_name FROM information_schema.tables WHERE table_name LIKE '%procrastinate%' LIMIT 5;
