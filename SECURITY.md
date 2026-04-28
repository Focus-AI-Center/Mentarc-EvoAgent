# Security Policy

## Deployment posture

EvoAgent is a **research artifact**. It is designed for single-tenant, trusted-network use alongside the paper. The codebase has **not** been audited for production security and should not be exposed to untrusted users or the public internet without significant hardening.

Several components ship with explicit demo-level implementations that are called out below. Production operators are expected to replace them with real alternatives.

## Known limitations (do not deploy publicly without mitigating)

### 1. `execute_code` tool — sandbox depends on deployment mode

[`core/plugins/execute_code.py`](core/plugins/execute_code.py) is a thin
dispatcher that delegates to one of two backends, picked at startup by
[`core/plugins/sandbox/__init__.py::make_sandbox_backend`](core/plugins/sandbox/__init__.py)
based on whether `MINIO_ENDPOINT` is set. The dispatcher itself enforces
language whitelist (bash / python only), a regex blacklist for destructive
patterns (`rm -rf /`, `mkfs`, `dd if=`, `shutdown`, fork bombs, raw-device
overwrite), CWD path validation (no traversal), 30 s default timeout, and
10 KB output truncation. Everything else depends on the backend.

#### MINIO mode → `RemoteDockerBackend` (validated, recommended)

Each invocation creates a one-shot Docker container on a separate sandbox
host:

- **Container**: `evoagent-sandbox:python3.11` (built from
  [`deploy/sandbox-image/`](deploy/sandbox-image)). Started with
  `--cap-add SYS_ADMIN --device /dev/fuse --security-opt apparmor=unconfined`
  (FUSE prerequisites for s3fs).
- **Workspace**: only the user's bucket prefix is mounted —
  `s3fs {bucket}:/{user_id} /workspace` — so the container's FUSE view
  starts at the user's root. `cd /workspace/../other_user` resolves above
  the FUSE mount (it lands on the container's own rootfs, not on s3fs),
  so cross-user paths are simply not reachable through the filesystem.
  Server-side writes via `MinIOSDKBackend` and sandbox-side writes through
  s3fs share the same object key space — strong consistency, no caches.
- **Lifecycle**: server runs `containers.run(detach=True, auto_remove=False)`,
  reads stdout/stderr after the container exits, then `force=True` removes
  it in `finally`. `--rm` is intentionally not used because we read logs
  after exit. Container leaks are possible if the server is `kill -9`'d
  mid-run; handle with a periodic `docker container prune -f --filter "until=1h"`
  on the sandbox host.
- **Credentials passed in (env)**: `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` /
  `MINIO_BUCKET` / `USER_ID`. M2 ships with the **server's root credentials**;
  this is the most permissive option and should be replaced before production
  exposure (see roadmap below).
- **Privilege drop before user code runs**. The entrypoint runs as root only
  long enough to write `/etc/passwd-s3fs` (mode 600 root) and start the s3fs
  mount; it then `unset`s `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` and
  `exec runuser -u sandbox --` into the user's command. The `sandbox` UID
  (2000, no home, no sudo) cannot read the cred file (mode 600) nor s3fs's
  `/proc/<pid>/environ` (root-owned), and `runuser ... -- env ...` resets the
  inherited env to a minimal allowlist (`HOME`, `LANG`, `LC_ALL`, plus
  Python-only flags). The s3fs mount is configured with
  `allow_other -o uid=2000 -o gid=2000` so reads / writes through `/workspace`
  still work for the unprivileged UID.

**What this protects against**

- Process / mount / network namespace separation between sandbox runs and the
  server (kernel-mediated, not VM-grade).
- Kernel escape from sandbox cannot reach the *server* host; it lands on
  the *sandbox* host, whose blast radius is whatever you've allowed it on
  the network and in your MinIO ACL.
- A misbehaving sandbox cannot eat the server's RAM / CPU / disk.

**What this does NOT protect against (yet)**

- **Cross-user MinIO access if a privileged process is compromised.** The
  s3fs mount is scoped to `{bucket}:/{user_id}` (FUSE view cannot reach
  other users' prefixes) and the cred env vars are scrubbed before user
  code runs as `sandbox` UID 2000 (so a Python `boto3` client inside the
  sandbox cannot read the keys from `os.environ`, `/etc/passwd-s3fs`, or
  `/proc/<pid>/environ`). What this does **not** stop: a kernel exploit
  that escalates back to root in the namespace can still read the cred
  file and the s3fs daemon's environ, which would re-expose the server's
  root credentials and grant access to any user prefix in the bucket. M3
  in the roadmap closes this last gap by replacing the env creds with
  STS tokens scoped to `{user_id}/*` with a 5-minute TTL — even a full
  privilege escalation then only buys 5 minutes of single-user blast
  radius.
- **Network egress.** `--network host` (the validated default) gives the
  container the same network reachability as the sandbox host. Tighten
  with a custom bridge + iptables/NetworkPolicy, or an egress proxy.
- **Kernel escape.** Containers are not VMs. For adversarial multi-tenant
  use, run the sandbox host on gVisor / Kata / Firecracker, or replace
  this backend with a managed runtime (E2B, Modal, Daytona).
- **Container leaks on server crash.** `auto_remove=False` + finally-remove
  works for clean exits and timeouts; `kill -9` on the server leaves an
  Exited container until pruned.

**Before exposing MINIO mode to untrusted users**

1. Implement STS scoping so the sandbox sees credentials scoped to
   `arn:aws:s3:::{bucket}/{user_id}/*` with a 5-minute TTL, instead of
   the server's root creds. MinIO requires the Allow + Deny-NotResource
   pattern (the AWS-standard single Allow does not work on MinIO 2024+).
2. Replace `--network host` with a bridge that only routes to the MinIO
   endpoint + DNS; default-deny everything else.
3. Add per-user concurrency + QPS caps in `execute_code.py` (see § 3).
4. Move the docker daemon onto a hardened runtime (gVisor preferred for
   density, Firecracker for adversarial isolation).
5. Persist an audit log of every `(user_id, code_hash, sandbox_label,
   return_code, stderr_tail)` tuple.

#### LOCAL mode → `LocalSubprocessBackend` (single-tenant only)

Same host as the server. The dispatcher hands the validated code to
`subprocess.run` with `cwd` set to the user's workspace directory under
`WORKSPACE_ROOT/{user_id}/`. There is **no kernel-level isolation** — no
namespaces, no seccomp filter, no resource limits beyond the
dispatcher-level wall-clock timeout. The only barriers are the
dispatcher's regex blacklist, cwd confinement (no `..`, no absolute
paths), the timeout, and output truncation.

**What this does NOT protect against**

- **Anything a normal subprocess can do as the server's UID**: read
  arbitrary host files the server can read, exhaust memory / CPU / disk,
  modify other users' workspaces under `WORKSPACE_ROOT`, exfiltrate LLM
  API keys from the environment, etc.
- **Network egress**: the subprocess inherits the host's network. Use a
  host-level firewall if you need to restrict it.
- **Non-Linux hosts**: works the same way (no platform-specific
  isolation to lose).

**Before exposing LOCAL mode to untrusted users**: don't. Switch to MINIO
mode first.

#### If you do not need `execute_code` at all

Remove it from [`core/plugins/__init__.py`](core/plugins/__init__.py)'s
`PLUGIN_CLASSES` and from `DEFAULT_PLUGINS`.

### 2. Demo-grade authentication

[`app/api/routers/auth.py`](app/api/routers/auth.py) implements register / login with:

- SHA-256 of `salt + password` (no bcrypt / argon2 / scrypt)
- A single process-wide salt sourced from `AUTH_SALT` env var (or a hardcoded fallback)
- Password hashes stored in plain JSON at `data/users.json`
- No session tokens, no password reset, no MFA, no lockout, no rate limit

This is sufficient for a single-user research laptop. It is **not** sufficient for anything else. Replace with a real auth provider (OIDC / SAML / Auth0 / Keycloak) or at minimum a bcrypt + JWT stack before any production use.

### 3. No rate limiting on `/api/agent/chat`

Agent requests fan out to paid LLM APIs. An unauthenticated or lightly-authenticated endpoint trivially becomes a pocketbook DDoS. Before exposing the server:

- Put it behind a reverse proxy with rate limits, **and/or**
- Add `slowapi` (or equivalent) middleware with per-user token budgets.

### 4. Permissive CORS by default

CORS defaults to `http://localhost:8194` / `http://127.0.0.1:8194` only. Override with `ALLOWED_ORIGINS` env var (comma-separated). Do not set `ALLOWED_ORIGINS="*"` on any deployment that serves authenticated users.

### 5. Workspace isolation is per-user directory, not per-tenant

The `Workspace` class gives each `user_id` its own subtree under `WORKSPACE_ROOT`. There is no cross-user ACL enforcement beyond the filesystem path construction. Someone who can craft an arbitrary `user_id` in an API request can read/write another user's workspace. Treat this as trusted-client-side.

## Reporting a vulnerability

If you believe you've found a security issue in EvoAgent:

1. **Do not** open a public GitHub issue.
2. Report privately via GitHub Security Advisories on the repository, or by email to the maintainers listed in [CITATION.cff](CITATION.cff).
3. Please include: affected version / commit, reproduction steps, and your assessment of impact.

We aim to acknowledge within 7 days and provide a status update within 30.

## Supported versions

This is a research codebase, not a maintained library. Security fixes will be applied to `main`. Historical tags (e.g. `v1.0-paper` when published) are frozen for reproducibility and will not receive updates.
