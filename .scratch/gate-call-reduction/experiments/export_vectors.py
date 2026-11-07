"""Read-only export of Gate-labelled fragment embeddings (runs inside the app container)."""

import json
import os
import sys
from array import array

import psycopg

sys.path.insert(0, "/app")
from src.processing.operational_semantics import _detect_service_families  # noqa: E402

QUERY = """
WITH last AS (
    SELECT DISTINCT ON (story_id) story_id, retention
    FROM story_event_triage_decisions
    WHERE created_at > now() - interval '7 days'
    ORDER BY story_id, created_at DESC
)
SELECT l.story_id, l.retention, f.id, extract(epoch from f.created_at),
       f.text_content, v.embedding::real[]
FROM last l
JOIN story_fragments sf ON sf.story_id = l.story_id
JOIN source_fragments f ON f.id = sf.fragment_id
JOIN source_fragment_embeddings sfe ON sfe.fragment_id = f.id
JOIN fragment_embedding_vectors v ON v.id = sfe.vector_id
"""

VECTOR_QUERY_SUFFIX = ""

meta = []
vecs = array("f")
with psycopg.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on") as conn:
    for story_id, retention, frag_id, ts, text, emb in conn.execute(QUERY):
        if not isinstance(emb, list):
            emb = [float(x) for x in str(emb).strip("[]").split(",")]
        meta.append(
            {
                "story_id": story_id,
                "label": retention,
                "fragment_id": frag_id,
                "ts": float(ts),
                "service": bool(_detect_service_families(text or "")),
            }
        )
        vecs.extend(emb)
with open("/tmp/knn_meta.json", "w", encoding="utf-8") as fh:
    json.dump(meta, fh, ensure_ascii=False)
with open("/tmp/knn_vecs.f32", "wb") as fh:
    vecs.tofile(fh)
print(f"rows={len(meta)} dims={len(vecs) // max(len(meta), 1)}")
