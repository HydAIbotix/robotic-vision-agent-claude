# Cloud-agnostic image for the Kiosk-QA backend (branch: cloud-agnostic-agent).
# ONE image runs everywhere — Azure, GCP, EC2, on-prem — because the model is a remote Claude
# call and every infra dependency (Postgres, MinIO/S3, Redis) is selected by env at runtime.
# The SAME image is used by docker-compose and by the Kubernetes manifests in deploy/k8s/.
FROM python:3.11-slim AS base

# System deps: OpenCV needs libGL/libglib; Playwright's Chromium pulls its own via `playwright
# install-deps`. Kept minimal to stay small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 curl \
        xvfb x11vnc novnc websockify \
        git \
    && rm -rf /var/lib/apt/lists/*
# xvfb/x11vnc/novnc/websockify power the OPTIONAL live-browser viewer (ENABLE_VNC=true): headed
# Chromium renders onto a virtual display that is streamed to a browser at :6080. Inert by default.
# git is used by Auto-Repair (repo-state guard, branch/commit, PR-prep) against the mounted POS repo.

# Node.js 20 + npm for the Auto-Repair verification step (tsc type-check + `npm run build`) run
# against the mounted POS repo. Copied from the official node image (same Debian bookworm base as
# python:3.11-slim), so we get node 20 without a NodeSource setup. Build fails fast if node is broken.
COPY --from=node:20-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

WORKDIR /app

# Install Python deps in a layer keyed ONLY on pyproject.toml, so editing app source
# (vision_agent/*, api/*, run_explorer.py, …) does NOT re-run this expensive step (torch,
# sentence-transformers, chromadb, Playwright's Chromium). The editable install needs the package
# dir to exist, so we create a STUB vision_agent/__init__.py here; the REAL source arrives via
# `COPY . .` below and the `-e` install (a .pth pointing at /app/vision_agent) then uses it. Result:
# a source-only change rebuilds in seconds (just `COPY . .`), not minutes.
COPY pyproject.toml README.md ./
RUN mkdir -p vision_agent && touch vision_agent/__init__.py
RUN pip install --no-cache-dir -e ".[cloud,playwright,repair]" \
    && python -m playwright install --with-deps chromium

# Now the real source. Editing anything here busts ONLY this layer (fast) — the dep layer stays cached.
COPY . .

# Non-hot-path defaults; override via the container's env / a mounted .env / a k8s ConfigMap.
ENV STORAGE_BACKEND=local \
    PERSISTENCE_BACKEND=sqlite \
    EVENT_BUS_BACKEND=memory \
    SERVICE_ROLE=all \
    API_HOST=0.0.0.0 \
    API_PORT=8001

EXPOSE 8001
EXPOSE 6080

# Dispatch by role: SERVICE_ROLE=worker runs the queue/Temporal consumer (`python -m worker`);
# anything else runs the API (uvicorn WITHOUT --reload, intentional — see CLAUDE.md). One image,
# two roles — the k8s manifests set SERVICE_ROLE per Deployment.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Strip any CR (the file may be authored on Windows) so the Linux shebang works, then make executable.
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod +x /usr/local/bin/docker-entrypoint.sh
CMD ["/usr/local/bin/docker-entrypoint.sh"]
