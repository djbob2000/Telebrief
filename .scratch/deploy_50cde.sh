#!/bin/bash
KEY=/Users/air/Downloads/ssh-key-2026-08-05.key
TAG=dev-50cde745fa94c25321fadda78693127316a9f818
for i in $(seq 1 40); do
  if ssh -i "$KEY" -o StrictHostKeyChecking=accept-new opc@92.5.58.200 "timeout 200 docker pull ghcr.io/djbob2000/telebrief:$TAG >/dev/null 2>&1"; then
    ssh -i "$KEY" -o StrictHostKeyChecking=accept-new opc@92.5.58.200 "cd /home/opc/Telebrief && IMAGE_TAG=$TAG timeout 500 docker compose up -d telebrief-app telebrief-worker telebrief-processing-worker telebrief-authority-worker >/dev/null 2>&1; docker inspect -f '{{.Config.Image}} {{.State.Health.Status}}' telebrief-processing-worker"
    echo "DEPLOYED $TAG"; exit 0
  fi
  echo "waiting $i"; sleep 60
done
echo TIMEOUT
