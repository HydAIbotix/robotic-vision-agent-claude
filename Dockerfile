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
    && rm -rf /var/lib/apt/lists/*
# xvfb/x11vnc/novnc/websockify power the OPTIONAL live-browser viewer (ENABLE_VNC=true): headed
# Chromium renders onto a virtual display that is streamed to a browser at :6080. Inert by default.

WORKDIR /app

# Install Python deps first for layer caching. The [cloud] extra adds psycopg + redis + boto3;
# [playwright] adds the browser driver used by exploration + playwright test runs.
COPY pyproject.toml README.md ./
COPY vision_agent/ ./vision_agent/
RUN pip install --no-cache-dir -e ".[cloud,playwright]" \
    && python -m playwright install --with-deps chromium

# Now the rest of the source (changes here don't bust the dep layer).
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
