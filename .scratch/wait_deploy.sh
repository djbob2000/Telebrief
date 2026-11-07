#!/bin/bash
SHA=$(cat /Users/air/develop/Telebrief/.scratch/deploy_sha.txt)
for i in $(seq 1 30); do
  img=$(ssh -i /Users/air/Downloads/ssh-key-2026-08-05.key -o StrictHostKeyChecking=accept-new opc@92.5.58.200 'docker inspect -f "{{.Config.Image}}" telebrief-processing-worker' 2>/dev/null)
  if echo "$img" | grep -q "${SHA:0:7}"; then echo "DEPLOYED $img"; exit 0; fi
  echo "waiting $i: $img"
  sleep 60
done
echo "TIMEOUT"
