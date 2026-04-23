# syntax=docker/dockerfile:1.7
#
# EvoAgent server image. Multi-stage build: the builder installs into a venv,
# the runtime stage copies only the venv + source and runs as a non-root user.
#
# Build:   docker build -t evoagent:latest .
# Run:     docker run --rm -p 8194:8194 --env-file .env evoagent:latest

ARG PYTHON_VERSION=3.11

# -------- builder --------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Dependencies first (better layer cache)
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# -------- runtime --------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PORT=8194

# Create unprivileged user + app directory
RUN groupadd --system --gid 1000 evoagent \
 && useradd  --system --uid 1000 --gid evoagent --create-home --home-dir /home/evoagent evoagent \
 && mkdir -p /app /app/data /app/logs \
 && chown -R evoagent:evoagent /app

# Copy installed venv from builder
COPY --from=builder --chown=evoagent:evoagent /opt/venv /opt/venv

WORKDIR /app
COPY --chown=evoagent:evoagent . /app

USER evoagent
EXPOSE 8194

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=3).status==200 else 1)"

# Gunicorn + uvicorn worker; `sh -c` so $PORT expands.
CMD ["sh", "-c", "gunicorn app.api.server:app \
     --bind 0.0.0.0:${PORT} \
     --workers 4 \
     --worker-class uvicorn.workers.UvicornWorker \
     --capture-output \
     --timeout 300 \
     --keep-alive 400"]
