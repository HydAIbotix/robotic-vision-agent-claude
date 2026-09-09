#!/usr/bin/env sh
# One image, two roles. SERVICE_ROLE=worker → the run-job consumer; otherwise → the API server.
set -e

if [ "$SERVICE_ROLE" = "worker" ]; then
  if [ "$ORCHESTRATOR_BACKEND" = "temporal" ]; then
    exec python -m worker --temporal
  fi
  exec python -m worker
fi

exec python -m uvicorn api.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8001}"
