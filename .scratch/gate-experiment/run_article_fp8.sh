#!/bin/sh
# Replays one frozen daily_article run on the :floor model filtered to fp8 (no delivery).
# The patched provider module is mounted over the image copy; nothing else changes.
set -u
RUN_ID="$1"
cd /home/opc/gate_experiment || exit 1
DBURL=$(docker exec telebrief-app printenv DATABASE_URL)
echo "== deepseek/deepseek-v4.1-flash-1017:floor fp8"
docker run --rm --network telebrief_default --env-file /home/opc/Telebrief/.env \
  -e DATABASE_URL="$DBURL" \
  -e OPENROUTER_MODEL="deepseek/deepseek-v4.1-flash-1017:floor" \
  -e OPENROUTER_MODEL_2= \
  -e OPENROUTER_FLOOR_QUANTIZATIONS=fp8 \
  -v /home/opc/Telebrief/config.yaml:/app/config.yaml:ro \
  -v /home/opc/gate_experiment/ai_providers.py:/app/src/ai_providers.py:ro \
  -v /home/opc/gate_experiment:/work \
  --entrypoint python ghcr.io/djbob2000/telebrief:dev-3f650869a716b97a08fe022dde25233aedd2afa6 \
  /app/scripts/preview_article.py --run-id "$RUN_ID" \
  --output "/work/out/article_deepseek_fp8.md" --diagnostics-output "/work/out/article_deepseek_fp8.diag.json" \
  > "out/article_deepseek_fp8.log" 2>&1
echo "exit=$?"
echo DONE
