#!/bin/sh
set -eu
# Secrets must already exist, readable only by the service account/root.
# Reuses the existing container; never restarts unrelated services.
if docker container inspect instasaver-api >/dev/null 2>&1; then
  docker start instasaver-api
  exit 0
fi
docker run -d --name instasaver-api --restart unless-stopped \
  --user "$(id -u instasaver):$(id -g instasaver)" \
  --entrypoint telegram-bot-api \
  --env-file /opt/instasaver/secrets/api.env \
  -p 127.0.0.1:8091:8081 \
  -v /opt/instasaver/api:/api \
  -v /opt/instasaver/data:/opt/instasaver/data:ro \
  --memory 768m --cpus 1 --cap-drop ALL --security-opt no-new-privileges \
  --log-opt max-size=10m --log-opt max-file=2 \
  aiogram/telegram-bot-api@sha256:a6cbb08dd181df11bbe4dc71ab8d74a6c97e0c1fa148f5c7c617affca42de9ef \
  --local --http-port=8081 --dir=/api --temp-dir=/api/tmp --verbosity=0
