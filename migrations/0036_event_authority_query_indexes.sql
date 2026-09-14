-- 0036_event_authority_query_indexes.sql
-- Targeted query indexes for Event-First authority selectors:
-- 1. Fast dirty cluster states retrieval ordered by last_seen_at DESC, story_id DESC.
-- 2. Fast event-first story edition lookup.
-- 3. Fast cutoff assignment resolution for event stories.
-- 4. Fast coverage verification on exact scope and triage decisions.

-- 1. Dirty cluster states scan ordered by recent activity
CREATE INDEX IF NOT EXISTS idx_story_cluster_state_dirty_last_seen
ON story_cluster_state(last_seen_at DESC, story_id DESC)
WHERE analysis_dirty = TRUE;

-- 2. Event-first stories by edition
CREATE INDEX IF NOT EXISTS idx_stories_event_first_edition
ON stories(edition_id, id)
WHERE knowledge_source = 'event_first';

-- 3. Cutoff assignment resolution by story ordered by assignment time
CREATE INDEX IF NOT EXISTS idx_story_fragments_story_assigned
ON story_fragments(story_id, assigned_at DESC, id DESC);

-- 4. Authority scope decisions covering lookup
CREATE INDEX IF NOT EXISTS idx_story_edition_scope_authority_lookup
ON story_edition_scope_decisions(story_id, latest_assignment_id, edition_id, scope_version, scope_config_hash);

-- 5. Authority triage decisions covering lookup
CREATE INDEX IF NOT EXISTS idx_story_event_triage_authority_lookup
ON story_event_triage_decisions(story_id, latest_assignment_id, triage_version, scope_config_hash, retention);
