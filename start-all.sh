#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start-all.sh — launch the ENTIRE cloud-agnostic solution on this VM at once.
#
#   [1] Kiosk POS (the app under test)  → nginx :80 + card-service
#         repo: robotics-kiosk-pos  (branch: expanded-cloud-agnostic)
#   [2] QA backend + agents             → FastAPI :8001 + worker
#         + Postgres + Redis + MinIO    (this repo: robotic-vision-agent-claude)
#
# Usage:
#   ~/robotic-vision-agent-claude/start-all.sh            # build + start both
#   POS_DIR=/path/to/robotics-kiosk-pos ./start-all.sh    # if the POS repo is elsewhere
#   PULL=1 ./start-all.sh                                 # git pull both repos first
#   LOCAL_REPAIR=1 ./start-all.sh                         # ALSO start the local Auto-Repair stack
#                                                         #   (Neo4j + Ollama, graphrag) and pull the
#                                                         #   configured local model. Off by default.
#
# The QA backend reaches the POS over the VM's INTERNAL IP (from inside the app
# container); browsers reach the POS over the EXTERNAL IP. Both are printed below.
# ---------------------------------------------------------------------------
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POS_DIR="${POS_DIR:-$HOME/robotics-kiosk-pos}"
STUDIO_DIR="${STUDIO_DIR:-$HOME/kiosk-test-studio}"   # optional operator UI (branch studio-cloud-agnostic)
LOCAL_REPAIR="${LOCAL_REPAIR:-0}"                     # 1 = also start Neo4j + Ollama (graphrag local Auto-Repair)

# Compose profile for the OPTIONAL local Auto-Repair backends (neo4j + ollama). Empty by default, so a
# normal run starts nothing extra (no regression); LOCAL_REPAIR=1 adds --profile local-repair.
PROFILE_ARG=""
[ "$LOCAL_REPAIR" = "1" ] && PROFILE_ARG="--profile local-repair"

# External IP (GCP metadata first, then a public echo as fallback).
EXTERNAL_IP="$(curl -s -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || true)"
[ -z "$EXTERNAL_IP" ] && EXTERNAL_IP="$(curl -s ifconfig.me 2>/dev/null || true)"
INTERNAL_IP="$(hostname -I | awk '{print $1}')"

echo "==> External IP: ${EXTERNAL_IP:-<unknown>}    Internal IP: ${INTERNAL_IP}"

if [ ! -d "$POS_DIR" ]; then
  echo "!! POS repo not found at '$POS_DIR'. Set POS_DIR=/path/to/robotics-kiosk-pos and retry." >&2
  exit 1
fi

if [ "${PULL:-0}" = "1" ]; then
  echo "==> git pull repos"
  ( cd "$POS_DIR"     && git pull --ff-only )
  ( cd "$BACKEND_DIR" && git pull --ff-only )
  [ -d "$STUDIO_DIR" ] && ( cd "$STUDIO_DIR" && git pull --ff-only )
fi

echo "==> [1/3] Kiosk POS    ($POS_DIR)"
( cd "$POS_DIR" && PUBLIC_BASE_URL="http://${EXTERNAL_IP}" docker compose up -d --build )

echo "==> [2/3] QA backend   ($BACKEND_DIR)${PROFILE_ARG:+  (+ local Auto-Repair: Neo4j + Ollama)}"
( cd "$BACKEND_DIR" && docker compose up -d --build $PROFILE_ARG )

echo "==> waiting for the API to answer…"
for _ in $(seq 1 40); do
  if curl -sf http://localhost:8001/api/health >/dev/null 2>&1; then echo "    API healthy."; break; fi
  sleep 3
done

# Local Auto-Repair prep: pull the CONFIGURED local model into Ollama (whatever REPAIR_LOCAL_MODEL /
# the config default resolves to — not hardcoded), so graphrag + a local Llama are ready to use.
if [ "$LOCAL_REPAIR" = "1" ]; then
  echo "==> Local Auto-Repair (graphrag): preparing Ollama + Neo4j"
  # The exact model the app will use: env override if set, else read the effective config from the app.
  MODEL="${REPAIR_LOCAL_MODEL:-}"
  if [ -z "$MODEL" ]; then
    MODEL="$( cd "$BACKEND_DIR" && docker compose exec -T app \
      python -c 'from vision_agent.config import settings; print(settings.repair_local_model)' 2>/dev/null | tr -d '\r\n' || true )"
  fi
  [ -z "$MODEL" ] && MODEL="qwen2.5-coder:14b"
  echo "    configured local model: $MODEL"
  # Wait for the Ollama server, then pull the model (no-op if already present; large models take a while).
  for _ in $(seq 1 20); do
    if ( cd "$BACKEND_DIR" && docker compose exec -T ollama ollama --version >/dev/null 2>&1 ); then break; fi
    sleep 3
  done
  echo "    pulling '$MODEL' into Ollama (first time can take several minutes)…"
  ( cd "$BACKEND_DIR" && docker compose exec -T ollama ollama pull "$MODEL" ) \
    || echo "    !! ollama pull failed — pull it manually: docker compose exec ollama ollama pull $MODEL"
  echo "    Ollama ready. NEXT: rebuild the RAG index into Neo4j against the on-disk code:"
  echo "        curl -X POST http://localhost:8001/api/repair/index"
  echo "    (or use the studio 'Rebuild index' button). Verify the active stack:"
  echo "        curl -s http://localhost:8001/api/health | grep -o '\"repair_[a-z_]*\":[^,]*'"
fi

if [ -d "$STUDIO_DIR" ]; then
  echo "==> [3/3] Test Studio  ($STUDIO_DIR)"
  ( cd "$STUDIO_DIR" && docker compose up -d --build )
else
  echo "==> [3/3] Test Studio  SKIPPED — clone it to $STUDIO_DIR (branch studio-cloud-agnostic) to enable the UI."
fi

echo
echo "======================================================================"
echo "  POS (app under test):  http://${EXTERNAL_IP}/?screenLayout=standard&flowMode=full"
echo "  Test Studio (UI)    :  http://${EXTERNAL_IP}:8080        (open tcp:8080 in the firewall)"
echo "  Live browser (noVNC):  http://${EXTERNAL_IP}:6080/vnc.html  (watch exploration/execution; open tcp:6080)"
echo "  QA API health       :  http://localhost:8001/api/health"
echo "                          (external, if 8001 is firewalled to your IP: http://${EXTERNAL_IP}:8001)"
if [ "$LOCAL_REPAIR" = "1" ]; then
echo "  Neo4j browser       :  http://${EXTERNAL_IP}:7474   (bolt://…:7687 · neo4j/neo4jpassword · open tcp:7474,7687)"
echo "  Local Auto-Repair   :  graphrag + Ollama running; rebuild the index, then run a failing test."
fi
echo
echo "  Explore the POS (kiosk_url uses the INTERNAL IP the app container can reach):"
echo "    curl -s -X POST http://localhost:8001/api/explore -H 'Content-Type: application/json' \\"
echo "      -d '{\"kiosk_id\":\"K-RPS\",\"kiosk_url\":\"http://${INTERNAL_IP}/?screenLayout=standard&flowMode=full\"}'"
echo "======================================================================"
( cd "$BACKEND_DIR" && docker compose ps )
