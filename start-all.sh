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
#
# The QA backend reaches the POS over the VM's INTERNAL IP (from inside the app
# container); browsers reach the POS over the EXTERNAL IP. Both are printed below.
# ---------------------------------------------------------------------------
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POS_DIR="${POS_DIR:-$HOME/robotics-kiosk-pos}"
STUDIO_DIR="${STUDIO_DIR:-$HOME/kiosk-test-studio}"   # optional operator UI (branch studio-cloud-agnostic)

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

echo "==> [2/3] QA backend   ($BACKEND_DIR)"
( cd "$BACKEND_DIR" && docker compose up -d --build )

echo "==> waiting for the API to answer…"
for _ in $(seq 1 40); do
  if curl -sf http://localhost:8001/api/health >/dev/null 2>&1; then echo "    API healthy."; break; fi
  sleep 3
done

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
echo "  QA API health       :  http://localhost:8001/api/health"
echo "                          (external, if 8001 is firewalled to your IP: http://${EXTERNAL_IP}:8001)"
echo
echo "  Explore the POS (kiosk_url uses the INTERNAL IP the app container can reach):"
echo "    curl -s -X POST http://localhost:8001/api/explore -H 'Content-Type: application/json' \\"
echo "      -d '{\"kiosk_id\":\"K-RPS\",\"kiosk_url\":\"http://${INTERNAL_IP}/?screenLayout=standard&flowMode=full\"}'"
echo "======================================================================"
( cd "$BACKEND_DIR" && docker compose ps )
