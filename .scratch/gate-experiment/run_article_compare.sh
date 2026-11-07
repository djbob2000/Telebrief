#!/bin/sh
# Replays one frozen daily_article run (no delivery) once per writer model. Runs on the server.
set -u
RUN_ID="$1"
cd /home/opc/gate_experiment || exit 1
DBURL=$(docker exec telebrief-app printenv DATABASE_URL)
for MODEL in "deepseek/deepseek-v4.1-flash-1017:floor" "openai/gpt-6-luna"; do
  SLUG=$(echo "$MODEL" | tr '/:' '__')
  echo "== $MODEL"
  docker run --rm --network telebrief_default --env-file /home/opc/Telebrief/.env \
    -e DATABASE_URL="$DBURL" \
    -e OPENROUTER_MODEL="$MODEL" \
    -e OPENROUTER_MODEL_2= \
    -v /home/opc/Telebrief/config.yaml:/app/config.yaml:ro \
    -v /home/opc/gate_experiment:/work \
    --entrypoint python ghcr.io/djbob2000/telebrief:dev-3f650869a716b97a08fe022dde25233aedd2afa6 \
    /app/scripts/preview_article.py --run-id "$RUN_ID" \
    --output "/work/out/article_${SLUG}.md" --diagnostics-output "/work/out/article_${SLUG}.diag.json" \
    > "out/article_${SLUG}.log" 2>&1
  echo "exit=$?"
done
echo DONE
