# EvoAgent sandbox image — one-shot container per execute_code invocation.
#
# Boot sequence (entrypoint.sh):
#   1. write MinIO creds to /etc/passwd-s3fs (mode 600)
#   2. s3fs mount $MINIO_BUCKET → /workspace
#   3. cd /workspace/$USER_ID/$REL_CWD
#   4. exec python -c "$CODE"  or  bash -c "$CODE"  under `timeout`
#   5. fusermount -u /workspace (always, via trap)
#
# Required runtime args (set by RemoteDockerBackend):
#   --cap-add SYS_ADMIN              (s3fs FUSE prerequisite)
#   --device /dev/fuse:/dev/fuse:rwm (s3fs FUSE prerequisite)
#   --security-opt apparmor=unconfined (Ubuntu/Debian default profile blocks fuse)
#   --network host                   (or bridge with route to MinIO)
#
# Required env vars:
#   MINIO_ENDPOINT   http://host:9000  (or https://...)
#   MINIO_ACCESS_KEY  / MINIO_SECRET_KEY
#   MINIO_BUCKET     evoagent-workspaces
#   USER_ID          per-user prefix inside the bucket
#   REL_CWD          (optional) subdirectory under the user's prefix
#   LANGUAGE         "python" or "bash"
#   TIMEOUT_S        kill the inner command after this many seconds
#   CODE             the actual code to execute
#
# Build:
#   cd deploy/sandbox-image
#   docker build -t evoagent-sandbox:python3.11 .
#
# Or remotely (when DOCKER_HOST=ssh://root@sandbox-host is set):
#   docker -H "$DOCKER_HOST" build -t evoagent-sandbox:python3.11 .

FROM python:3.11-slim

# s3fs + minimal shell tooling for the entrypoint.
# `fuse3` provides /usr/bin/fusermount3; we symlink it as `fusermount` for
# compatibility with Alibaba Linux / older Debian where the package is named
# differently. `procps` ships ps/kill which the timeout / trap path expects.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        s3fs \
        fuse \
        bash \
        coreutils \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pre-install the whitelisted scientific stack so cold-start does not hit
# pip/network. Keep this list deliberately narrow — anything else the user
# can `pip install` from inside their code (network is allowed by default).
# Use Aliyun mirror because the build host (China region) cannot reliably
# reach pypi.org.
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip config set global.trusted-host mirrors.aliyun.com \
    && pip install --no-cache-dir \
        numpy \
        pandas \
        requests \
        httpx \
    && rm -rf /root/.cache/pip

COPY entrypoint.sh /usr/local/bin/sandbox-entrypoint.sh
RUN chmod +x /usr/local/bin/sandbox-entrypoint.sh

# Force unbuffered stdout/stderr so container.logs() in the SDK sees output
# as it is written (not when the container exits).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint.sh"]
