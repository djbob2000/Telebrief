#!/bin/bash
KEY=/Users/air/Downloads/ssh-key-2026-08-05.key
for i in $(seq 1 60); do
  out=$(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new opc@92.5.58.200 'docker exec -i telebrief-postgres sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -At -F \"=\""' < /Users/air/develop/Telebrief/.scratch/q11.sql 2>/dev/null | grep -E "^(failed|pending|running|queued_proc)=" | tr '\n' ' ')
  echo "poll $i $(date -u +%H:%M:%S): $out"
  failed=$(echo "$out" | sed -n 's/.*failed=\([0-9]*\).*/\1/p')
  pending=$(echo "$out" | sed -n 's/.*pending=\([0-9]*\).*/\1/p')
  queued=$(echo "$out" | sed -n 's/.*queued_proc=\([0-9]*\).*/\1/p')
  if [ -n "$failed" ] && [ "$failed" -le 100 ] && [ "$pending" -le 100 ] && [ "$queued" -eq 0 ]; then echo "BACKLOG_DRAINED"; exit 0; fi
  sleep 240
done
echo "TIMEOUT"
