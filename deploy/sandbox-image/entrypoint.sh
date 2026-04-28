#!/bin/bash
# Sandbox entrypoint — mounts the user's MinIO bucket prefix and executes code.
#
# Reads env: MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET,
#            USER_ID, REL_CWD, LANGUAGE, TIMEOUT_S, CODE
# Writes:    stdout / stderr (captured by docker logs)
# Exit:      the inner command's return code (124 = timed out)
#
# Privilege model:
#   - This script runs as root (needs CAP_SYS_ADMIN for FUSE).
#   - After s3fs is mounted, the MINIO_*_KEY env vars are unset and the user's
#     code is exec'd as the unprivileged `sandbox` UID via runuser.
#   - As `sandbox`, the user's code cannot read /etc/passwd-s3fs (mode 600
#     root) nor /proc/<s3fs-pid>/environ (root-owned), so the MinIO root
#     credentials are not reachable from inside the executed code.

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

# Mount only the user's bucket prefix, not the whole bucket. s3fs supports
# the `bucket:/prefix` syntax — `/workspace` then *is* the user's root, and
# POSIX path resolution above FUSE means `cd ..` lands on the container's
# own rootfs, not on another user's prefix. This closes the accidental
# cross-user view; it does NOT prevent the in-container code from reading
# MINIO_ACCESS_KEY/SECRET_KEY from the env and signing its own S3 requests
# directly — that's M3's job (STS-scoped temporary credentials).
#
#   use_path_request_style — required for MinIO (vhost-style only works
#                            with proper DNS for the bucket).
#   nonempty               — silence the warning when /workspace exists.
#   compat_dir             — treat prefixes as directories without requiring
#                            a zero-byte directory marker object. The server
#                            writes nested keys like `{user_id}/prompts/X.md`
#                            but never materialises `{user_id}/` itself, so
#                            without this flag s3fs's startup HEAD on the
#                            prefix returns 404 and refuses to mount.
#   allow_other            — let non-mounter UIDs (i.e. our `sandbox` user)
#                            access the FUSE mount. Allowed without host
#                            /etc/fuse.conf changes because the mounter
#                            here is root.
#   uid=2000 / gid=2000    — surface all entries as owned by the sandbox
#                            user so it can write through the mount without
#                            chown of every directory.
#   url=...                — explicit MinIO endpoint.
SANDBOX_UID=$(id -u sandbox)
SANDBOX_GID=$(id -g sandbox)
s3fs "$MINIO_BUCKET:/$USER_ID" /workspace \
    -o url="$MINIO_ENDPOINT" \
    -o use_path_request_style \
    -o passwd_file="$PASSWD" \
    -o nonempty \
    -o compat_dir \
    -o allow_other \
    -o "uid=$SANDBOX_UID" \
    -o "gid=$SANDBOX_GID" \
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

# Resolve the working directory. /workspace is already scoped to the user.
# REL_CWD is validated by the server (no `..`, no leading slash); mkdir -p
# in case the dir was implied by a key but never materialised as a marker.
TARGET_CWD="/workspace"
if [ -n "${REL_CWD:-}" ]; then
    TARGET_CWD="/workspace/$REL_CWD"
    mkdir -p "$TARGET_CWD"
fi
cd "$TARGET_CWD" || { echo "sandbox-entrypoint: cd $TARGET_CWD failed" >&2; exit 67; }

# Strip MinIO credentials before handing control to user code. After this
# point /etc/passwd-s3fs (mode 600 root) and /proc/<s3fs-pid>/environ
# (root-owned) are the only places the keys live, and `sandbox` UID can
# read neither.
unset MINIO_ACCESS_KEY MINIO_SECRET_KEY

# Execute as the unprivileged sandbox user. `runuser -u sandbox --` resets
# the environment to a minimal default; we forward only what the inner
# command needs. `timeout` from coreutils sends SIGTERM after TIMEOUT_S,
# then SIGKILL after a 5s grace. Exit 124 = timed out (well-known
# coreutils code).
case "$LANGUAGE" in
    python)
        exec runuser -u sandbox -- env \
            HOME=/workspace LANG=C.UTF-8 LC_ALL=C.UTF-8 \
            PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
            timeout --kill-after=5 "$TIMEOUT_S" python -c "$CODE"
        ;;
    bash|sh|shell)
        exec runuser -u sandbox -- env \
            HOME=/workspace LANG=C.UTF-8 LC_ALL=C.UTF-8 \
            timeout --kill-after=5 "$TIMEOUT_S" bash -c "$CODE"
        ;;
    *)
        echo "sandbox-entrypoint: unsupported LANGUAGE=$LANGUAGE" >&2
        exit 68
        ;;
esac
