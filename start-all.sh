#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start-all.sh — launch the ENTIRE cloud-agnostic solution on this VM at once.
#
#   [1] Kiosk POS (the app under test)  → nginx :80 + card-service
#         repo: robotics-kiosk-pos  (branch: expanded-cloud-agnostic)
#   [2] QA backend + agents             → FastAPI :8001 + worker
#         + Postgres + Redis + MinIO    (this repo: robotic-vision-agent-claude)
#
# ===========================================================================
# THE THREE AUTO-REPAIR STACKS — exact command + the env values each one sets
# ===========================================================================
# You pick a stack with the LEFT-HAND env on the command line; start-all.sh then EXPORTS the
# RIGHT-HAND values for the app container (compose reads them as ${VAR:-default}). Nothing else changes.
#
# ── CASE 1 — Default: Chroma + Claude (cloud; no extra services) ────────────────────────────────
#     ./start-all.sh
#   Effective values:
#     LOCAL_REPAIR=0            (nothing exported)
#     REPAIR_RETRIEVAL_BACKEND = chroma      REPAIR_LLM_BACKEND = claude
#     (no neo4j/ollama, no GPU, REPAIR_LOCAL_* and INSTALL_MSGRAPHRAG unused)
#
# ── CASE 2 — Neo4j graph-RAG + local Llama (data-sovereign; CPU-friendly) ───────────────────────
#     LOCAL_REPAIR=graphrag REPAIR_LOCAL_MODEL=llama3.2:3b ./start-all.sh      # add GPU=1 to use the GPU
#   Effective values (exported by the script):
#     COMPOSE_PROFILES         = local-repair          (starts neo4j + ollama)
#     REPAIR_RETRIEVAL_BACKEND = graphrag              REPAIR_LLM_BACKEND = local
#     REPAIR_LOCAL_ONLY        = true   (air-gap)      REPAIR_LOCAL_MODEL = llama3.2:3b  (default if omitted)
#     INSTALL_MSGRAPHRAG       = (unset → false)       GPU = 0  (set GPU=1 to run Ollama on the GPU)
#
# ── CASE 3 — REAL Microsoft GraphRAG + Qwen coder (best quality; GPU) ────────────────────────────
#     GPU=1 LOCAL_REPAIR=msgraphrag REPAIR_LOCAL_MODEL=qwen2.5-coder:14b ./start-all.sh
#   Effective values (exported by the script):
#     GPU                      = 1                     (layers docker-compose.gpu.yml → Ollama on the L4)
#     COMPOSE_PROFILES         = local-repair          (starts neo4j + ollama)
#     REPAIR_RETRIEVAL_BACKEND = msgraphrag            REPAIR_LLM_BACKEND = local
#     REPAIR_LOCAL_ONLY        = true   (air-gap)      REPAIR_LOCAL_MODEL = qwen2.5-coder:14b  (default 7b if omitted)
#     INSTALL_MSGRAPHRAG       = true   (bakes the GraphRAG deps into the image; needs --build)
#     GRAPHRAG_EMBEDDING_MODEL = nomic-embed-text (compose default, pulled)   GRAPHRAG_EXPORT_NEO4J = true
#   → one Ollama model (qwen2.5-coder:14b) BUILDS the graph AND fixes the code; the graph is also loaded
#     into Neo4j so you can browse it (http://<EXTERNAL_IP>:7474).
#
# Notes: both local stacks are AIR-GAPPED by default (REPAIR_LOCAL_ONLY=true — nothing leaves the box);
#   set REPAIR_LOCAL_ONLY=false to keep Claude as a fallback. Any RIGHT-HAND value is overridable on the
#   command line (they use the SAME names the compose file reads). Other flags:
#     POS_DIR=/path ./start-all.sh   (POS repo elsewhere)     PULL=1 ./start-all.sh   (git pull first)
#
# The QA backend reaches the POS over the VM's INTERNAL IP (from inside the app
# container); browsers reach the POS over the EXTERNAL IP. Both are printed below.
# ---------------------------------------------------------------------------
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POS_DIR="${POS_DIR:-$HOME/robotics-kiosk-pos}"
STUDIO_DIR="${STUDIO_DIR:-$HOME/kiosk-test-studio}"   # optional operator UI (branch studio-cloud-agnostic)
LOCAL_REPAIR="${LOCAL_REPAIR:-0}"                     # 0/off | graphrag(=1) | msgraphrag(=2)

# Normalize LOCAL_REPAIR into a retrieval-backend mode. The two local modes differ ONLY in which
# retrieval backend + model they select — the seam is identical, so there is no rework switching.
REPAIR_MODE=""
case "$LOCAL_REPAIR" in
  0|off|"")            REPAIR_MODE="" ;;
  1|graphrag)          REPAIR_MODE="graphrag" ;;
  2|msgraphrag|microsoft) REPAIR_MODE="msgraphrag" ;;
  *) echo "!! Unknown LOCAL_REPAIR='$LOCAL_REPAIR' (use 0 | graphrag | msgraphrag)" >&2; exit 1 ;;
esac

# Compose profile for the OPTIONAL local Auto-Repair backends (neo4j + ollama). Nothing extra starts by
# default (no regression); a local mode selects the `local-repair` profile via the COMPOSE_PROFILES env
# var (NOT the `--profile` flag — its position after `up` is rejected on some compose versions, and the
# env var is also seen by the later `exec ollama` calls) and EXPORTS the env the compose file reads
# (${VAR:-default}) so the app container switches with no file edits.
if [ -n "$REPAIR_MODE" ]; then
  export COMPOSE_PROFILES="local-repair"
  export REPAIR_RETRIEVAL_BACKEND="$REPAIR_MODE"
  export REPAIR_LLM_BACKEND="local"
  export REPAIR_LOCAL_ONLY="${REPAIR_LOCAL_ONLY:-true}"     # air-gap by default; override to false to keep Claude backup
  if [ "$REPAIR_MODE" = "msgraphrag" ]; then
    export REPAIR_LOCAL_MODEL="${REPAIR_LOCAL_MODEL:-qwen2.5-coder:7b}"   # code model builds the graph AND fixes
    export INSTALL_MSGRAPHRAG="true"                        # bake the Microsoft GraphRAG deps (image build)
  else
    export REPAIR_LOCAL_MODEL="${REPAIR_LOCAL_MODEL:-llama3.2:3b}"        # lightweight Llama for the CPU demo
  fi
  echo "==> Local Auto-Repair mode: $REPAIR_MODE  (model: $REPAIR_LOCAL_MODEL, air-gap: ${REPAIR_LOCAL_ONLY})"
fi

# GPU overlay for Ollama (e.g. the L4 on a GCE g2-* machine). GPU=1 layers docker-compose.gpu.yml so the
# local model + the Microsoft GraphRAG build run on the GPU instead of the CPU (qwen 14b on CPU is
# impractical). We pre-check that Docker can actually reach the GPU and, if not, print the fix and keep
# going on CPU rather than failing the whole launch. Only meaningful with a local-repair mode.
COMPOSE_GPU=""
if [ "${GPU:-0}" = "1" ] && [ -n "$REPAIR_MODE" ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && docker info 2>/dev/null | grep -qi 'nvidia'; then
    COMPOSE_GPU="-f docker-compose.yml -f docker-compose.gpu.yml"
    echo "==> GPU enabled for Ollama (docker-compose.gpu.yml)."
  else
    echo "!! GPU=1 but Docker cannot see an NVIDIA GPU. Install the driver + nvidia-container-toolkit and" >&2
    echo "   verify:  docker run --rm --gpus all ollama/ollama:latest nvidia-smi  — then re-run with GPU=1." >&2
    echo "   Continuing on CPU for now (local inference will be VERY slow)." >&2
  fi
fi

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

echo "==> [2/3] QA backend   ($BACKEND_DIR)${REPAIR_MODE:+  (+ local Auto-Repair: Neo4j + Ollama)}${COMPOSE_GPU:+  (GPU)}"
( cd "$BACKEND_DIR" && docker compose $COMPOSE_GPU up -d --build )

echo "==> waiting for the API to answer…"
for _ in $(seq 1 40); do
  if curl -sf http://localhost:8001/api/health >/dev/null 2>&1; then echo "    API healthy."; break; fi
  sleep 3
done

# Local Auto-Repair prep: pull the selected model(s) into Ollama so the chosen local stack is ready.
if [ -n "$REPAIR_MODE" ]; then
  [ "$REPAIR_MODE" = "graphrag" ] && _svc=" + Neo4j" || _svc=""
  echo "==> Local Auto-Repair ($REPAIR_MODE): preparing Ollama${_svc}"
  MODEL="${REPAIR_LOCAL_MODEL:-qwen2.5-coder:7b}"    # exported above per mode
  echo "    local model: $MODEL"
  # Wait for the Ollama server, then pull the model (no-op if already present; large models take a while).
  for _ in $(seq 1 20); do
    if ( cd "$BACKEND_DIR" && docker compose exec -T ollama ollama --version >/dev/null 2>&1 ); then break; fi
    sleep 3
  done
  echo "    pulling '$MODEL' into Ollama (first time can take several minutes)…"
  ( cd "$BACKEND_DIR" && docker compose exec -T ollama ollama pull "$MODEL" ) \
    || echo "    !! ollama pull failed — pull it manually: docker compose exec ollama ollama pull $MODEL"
  # The Microsoft GraphRAG pipeline also needs an EMBEDDING model to build the graph.
  if [ "$REPAIR_MODE" = "msgraphrag" ]; then
    EMBED_MODEL="${GRAPHRAG_EMBEDDING_MODEL:-nomic-embed-text}"
    echo "    pulling GraphRAG embedding model '$EMBED_MODEL'…"
    ( cd "$BACKEND_DIR" && docker compose exec -T ollama ollama pull "$EMBED_MODEL" ) \
      || echo "    !! embed pull failed — pull it manually: docker compose exec ollama ollama pull $EMBED_MODEL"
  fi
  echo "    Ollama ready. NEXT: build the index against the on-disk code (this RUNS the $REPAIR_MODE pipeline):"
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
if [ "$REPAIR_MODE" = "graphrag" ]; then
echo "  Neo4j browser       :  http://${EXTERNAL_IP}:7474   (bolt://…:7687 · neo4j/neo4jpassword · open tcp:7474,7687)"
echo "  Local Auto-Repair   :  graphrag + Ollama ($REPAIR_LOCAL_MODEL); build the index, then run a failing test."
elif [ "$REPAIR_MODE" = "msgraphrag" ]; then
echo "  Local Auto-Repair   :  Microsoft GraphRAG + Ollama ($REPAIR_LOCAL_MODEL); build the index (runs the"
echo "                          entity/community pipeline — slow on CPU), then run a failing test. Graph"
echo "                          workspace: ./data/graphrag."
fi
echo
echo "  Explore the POS (kiosk_url uses the INTERNAL IP the app container can reach):"
echo "    curl -s -X POST http://localhost:8001/api/explore -H 'Content-Type: application/json' \\"
echo "      -d '{\"kiosk_id\":\"K-RPS\",\"kiosk_url\":\"http://${INTERNAL_IP}/?screenLayout=standard&flowMode=full\"}'"
echo "======================================================================"
( cd "$BACKEND_DIR" && docker compose ps )
