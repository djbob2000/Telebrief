-- A short chat reply is routed to its parent's Story by the reply link, because
-- its vector mostly reflects chat register. Record that route as its own kind.
ALTER TABLE story_fragments
    DROP CONSTRAINT IF EXISTS story_fragments_assignment_kind_check;

ALTER TABLE story_fragments
    ADD CONSTRAINT story_fragments_assignment_kind_check
    CHECK (assignment_kind IN ('new_story', 'vector_join', 'reply_join', 'manual'));

-- Gate reads the messages around a Story in chronological order, per source.
CREATE INDEX IF NOT EXISTS idx_source_items_source_observed
ON source_items (source_id, (COALESCE(published_at, first_collected_at)));
