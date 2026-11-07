#!/bin/bash
KEY=/Users/air/Downloads/ssh-key-2026-08-05.key
for i in $(seq 1 10); do
  out=$(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new opc@92.5.58.200 'docker exec -i telebrief-postgres sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -At -F \"|\""' <<'SQL' 2>&1
SELECT 'emb', count(*) FROM fragment_embedding_vectors WHERE created_at > now() - interval '5 minutes';
SELECT 'triage_ok', count(*) FROM story_event_triage_runs WHERE started_at > now() - interval '5 minutes' AND status = 'succeeded';
SELECT 'triage_fail', count(*) FROM story_event_triage_runs WHERE started_at > now() - interval '5 minutes' AND status <> 'succeeded';
SELECT 'queue_todo', count(*) FROM procrastinate.procrastinate_jobs WHERE status IN ('todo','doing');
SELECT 'jobs_failed_recent', count(*) FROM procrastinate.procrastinate_jobs WHERE status = 'failed' AND scheduled_at > now() - interval '5 minutes';
SQL
)
  echo "poll $i $(date -u +%H:%M:%S): $(echo "$out" | tr '\n' ' ')"
  sleep 60
done
