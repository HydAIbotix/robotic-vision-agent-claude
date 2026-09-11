#!/usr/bin/env sh
# One image, two roles. SERVICE_ROLE=worker → the run-job consumer; otherwise → the API server.
#
# When ENABLE_VNC=true, a virtual display + a browser-based VNC viewer are started FIRST, so the
# (headed) Chromium that App Explorer and test execution drive is watchable live in a browser at
# :6080/vnc.html. On a headless VM there is no real display, so we render Chromium onto an Xvfb
# virtual screen and stream that screen via x11vnc + noVNC. Gated by ENABLE_VNC, so the default
# (headless, no VNC) path is completely unchanged.
set -e

# Auto-Repair edits/commits the MOUNTED POS repo (owned by the host user, while the container runs as
# root) → git refuses with "detected dubious ownership" unless the path is marked safe. No-op if unset.
if [ -n "${REPAIR_CODEBASE_DIR:-}" ]; then
  git config --global --add safe.directory "$REPAIR_CODEBASE_DIR" 2>/dev/null || true
fi

if [ "${ENABLE_VNC:-false}" = "true" ]; then
  DISPLAY_NUM="${VNC_DISPLAY:-:99}"
  export DISPLAY="$DISPLAY_NUM"
  # Chromium must run HEADED to draw onto the virtual display (headless renders nothing to watch).
  export PLAYWRIGHT_HEADLESS=false
  echo "[entrypoint] ENABLE_VNC=true -> Xvfb ${DISPLAY_NUM} + x11vnc + noVNC on :6080"
  rm -f "/tmp/.X${DISPLAY_NUM#:}-lock" 2>/dev/null || true
  Xvfb "$DISPLAY_NUM" -screen 0 "${VNC_RESOLUTION:-1600x1000x24}" -nolisten tcp >/tmp/xvfb.log 2>&1 &
  # wait for the X socket before anything tries to use the display
  i=0
  while [ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ] && [ "$i" -lt 50 ]; do i=$((i + 1)); sleep 0.2; done
  # expose the virtual display over VNC (no password; the port is firewalled to your IP)
  x11vnc -display "$DISPLAY_NUM" -forever -shared -nopw -rfbport 5900 -quiet -bg >/tmp/x11vnc.log 2>&1
  # noVNC static web client + websockify bridge → browser viewer at http://<host>:6080/vnc.html
  ( websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 & )
fi

if [ "$SERVICE_ROLE" = "worker" ]; then
  if [ "$ORCHESTRATOR_BACKEND" = "temporal" ]; then
    exec python -m worker --temporal
  fi
  exec python -m worker
fi

exec python -m uvicorn api.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8001}"
