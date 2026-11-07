#!/bin/sh
# Replays one frozen weekly run with the patched article modules mounted over the image (read-only, no delivery).
set -u
RUN_ID="$1"
TAG="$2"
cd /home/opc/gate_experiment || exit 1
DBURL=$(docker exec telebrief-app printenv DATABASE_URL)
P=/home/opc/gate_experiment/patch
MOUNTS=""
for f in ai_providers.py publication/article_kitchen.py publication/article_validator.py publication/article_finalization.py publication/article_claims.py publication/article_semantic_support.py publication/article_editor.py publication/generation.py publication/article_quality.py; do
  MOUNTS="$MOUNTS -v $P/src/$f:/app/src/$f:ro"
done
docker run --rm --network telebrief_default --env-file /home/opc/Telebrief/.env \
  -e DATABASE_URL="$DBURL" \
  -v /home/opc/Telebrief/config.yaml:/app/config.yaml:ro \
  $MOUNTS \
  -v /home/opc/gate_experiment:/work \
  --entrypoint python ghcr.io/djbob2000/telebrief:dev /app/scripts/preview_article.py --run-id "$RUN_ID" \
  --output "/work/out/week${RUN_ID}_${TAG}.md" --diagnostics-output "/work/out/week${RUN_ID}_${TAG}.diag.json" \
  > "out/week${RUN_ID}_${TAG}.log" 2>&1
echo "exit=$?"
