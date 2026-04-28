#!/bin/bash
# Sandbox entrypoint — mounts the user's MinIO bucket prefix and executes code.
#
# Reads env: MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET,
#            USER_ID, REL_CWD, LANGUAGE, TIMEOUT_S, CODE
# Writes:    stdout / stderr (captured by docker logs)
# Exit:      the inner command's return code (124 = timed out)

set -u

# Validate required env up front. Failing early gives the dispatcher a clear
# stderr message instead of a confusing s3fs / cd error later.
for v in MINIO_ENDPOINT MINIO_ACCESS_KEY MINIO_SECRET_KEY MINIO_BUCKET USER_ID LANGUAGE TIMEOUT_S CODE; do
    if [ -z "${!v:-}" ]; then
        echo "sandbox-entrypoint: required env $v is missing" >&2
        exit 64
    fi
done

# Always unmount on exit so a crashed sandbox does not leak the FUSE mount
# inside the container's namespace (--rm cleans the container itself, but
# the mount counts toward the kernel's fuse instance count if we leave it).
cleanup() {
    if mountpoint -q /workspace 2>/dev/null; then
        fusermount -u /workspace 2>/dev/null \
            || fusermount3 -u /workspace 2>/dev/null \
            || umount /workspace 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Build the s3fs creds file. Mode 600 is required by s3fs (it refuses to
# proceed with a world-readable passwd file).
PASSWD=/etc/passwd-s3fs
printf '%s:%s\n' "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" > "$PASSWD"
chmod 600 "$PASSWD"

mkdir -p /workspace

# Mount.
#   use_path_request_style — required for MinIO (vhost-style only works
#                            with proper DNS for the bucket).
#   nonempty               — silence the warning when /workspace exists.
#   url=...                — explicit MinIO endpoint.
s3fs "$MINIO_BUCKET" /workspace \
    -o url="$MINIO_ENDPOINT" \
    -o use_path_request_style \
    -o passwd_file="$PASSWD" \
    -o nonempty \
    || { echo "sandbox-entrypoint: s3fs mount failed" >&2; exit 65; }

# Wait briefly for the mount to be ready. s3fs returns before the FUSE
# instance is fully wired in some kernels; readdir() will fail until then.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if mountpoint -q /workspace; then break; fi
    sleep 0.1
done
if ! mountpoint -q /workspace; then
    echo "sandbox-entrypoint: /workspace not mounted after wait" >&2
    exit 66
fi

# Make sure the user prefix exists. s3fs lazily creates directory markers on
# first mkdir; this is harmless on first run because Workspace._init_workspace
# has already written the sync_version key.
USER_DIR="/workspace/$USER_ID"
mkdir -p "$USER_DIR"

# Resolve the working directory. REL_CWD is validated by the server (no `..`,
# no leading slash); we still mkdir -p in case the dir was implied by a key
# but never actually materialised as a directory marker.
TARGET_CWD="$USER_DIR"
if [ -n "${REL_CWD:-}" ]; then
    TARGET_CWD="$USER_DIR/$REL_CWD"
    mkdir -p "$TARGET_CWD"
fi
cd "$TARGET_CWD" || { echo "sandbox-entrypoint: cd $TARGET_CWD failed" >&2; exit 67; }

# Execute. `timeout` from coreutils sends SIGTERM after TIMEOUT_S, then
# SIGKILL after a 5s grace. Exit 124 = timed out (well-known coreutils code).
case "$LANGUAGE" in
    python)
        timeout --kill-after=5 "$TIMEOUT_S" python -c "$CODE"
        ;;
    bash|sh|shell)
        timeout --kill-after=5 "$TIMEOUT_S" bash -c "$CODE"
        ;;
    *)
        echo "sandbox-entrypoint: unsupported LANGUAGE=$LANGUAGE" >&2
        exit 68
        ;;
esac
