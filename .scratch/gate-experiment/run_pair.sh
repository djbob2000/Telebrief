#!/bin/sh
# One frozen daily_article run replayed twice on the :floor model: fp4 (no filter) and fp8.
# Writes out/r<RUN>_fp4.md and out/r<RUN>_fp8.md. Nothing is delivered.
set -u
RUN_ID="$1"
cd /home/opc/gate_experiment || exit 1
DBURL=$(docker exec telebrief-app printenv DATABASE_URL)
IMAGE=ghcr.io/djbob2000/telebrief:dev-3f650869a716b97a08fe022dde25233aedd2afa6
MODEL="deepseek/deepseek-v4.1-flash-1017:floor"

echo "== r$RUN_ID fp4"
docker run --rm --network telebrief_default --env-file /home/opc/Telebrief/.env \
  -e DATABASE_URL="$DBURL" -e OPENROUTER_MODEL="$MODEL" -e OPENROUTER_MODEL_2= \
  -v /home/opc/Telebrief/config.yaml:/app/config.yaml:ro \
  -v /home/opc/gate_experiment:/work \
  --entrypoint python "$IMAGE" /app/scripts/preview_article.py --run-id "$RUN_ID" \
  --output "/work/out/r${RUN_ID}_fp4.md" --diagnostics-output "/work/out/r${RUN_ID}_fp4.diag.json" \
  > "out/r${RUN_ID}_fp4.log" 2>&1
echo "exit=$?"

echo "== r$RUN_ID fp8"
docker run --rm --network telebrief_default --env-file /home/opc/Telebrief/.env \
  -e DATABASE_URL="$DBURL" -e OPENROUTER_MODEL="$MODEL" -e OPENROUTER_MODEL_2= \
  -e OPENROUTER_FLOOR_QUANTIZATIONS=fp8 \
  -v /home/opc/Telebrief/config.yaml:/app/config.yaml:ro \
  -v /home/opc/gate_experiment/ai_providers.py:/app/src/ai_providers.py:ro \
  -v /home/opc/gate_experiment:/work \
  --entrypoint python "$IMAGE" /app/scripts/preview_article.py --run-id "$RUN_ID" \
  --output "/work/out/r${RUN_ID}_fp8.md" --diagnostics-output "/work/out/r${RUN_ID}_fp8.diag.json" \
  > "out/r${RUN_ID}_fp8.log" 2>&1
echo "exit=$?"
echo "DONE $RUN_ID"
