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
- **Workspace**: the MinIO bucket is mounted via s3fs at `/workspace` and
  the entrypoint `cd`s into `/workspace/{user_id}/{cwd}`. Server-side
  writes via `MinIOSDKBackend` and sandbox-side writes through s3fs share
  the same object key space — strong consistency, no caches.
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

**What this protects against**

- Process / mount / network namespace separation between sandbox runs and the
  server (kernel-mediated, not VM-grade).
- Kernel escape from sandbox cannot reach the *server* host; it lands on
  the *sandbox* host, whose blast radius is whatever you've allowed it on
  the network and in your MinIO ACL.
- A misbehaving sandbox cannot eat the server's RAM / CPU / disk.

**What this does NOT protect against (yet)**

- **Cross-user MinIO access from sandbox.** M2 hands the server's root
  creds to the container. Code in the sandbox can read/write any prefix
  in the bucket. M3 in the roadmap swaps these for STS tokens scoped to
  `{user_id}/*` with a 5-minute TTL.
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

#### LOCAL mode → `LocalBwrapBackend` (legacy, single-tenant only)

The pre-M2 path: same host as the server, bwrap rw-binds the user's
workspace and ro-binds `sys.prefix` + standard system paths, unshares
user / ipc / pid / uts / cgroup namespaces, and enforces `RLIMIT_AS`
(default 512 MB) + `RLIMIT_CPU` (= timeout) via `prlimit`. Network access
is allowed by default; disable via `EXECUTE_CODE_NETWORK=deny`. Force or
disable bwrap via `EXECUTE_CODE_SANDBOX=bubblewrap|off` (this env var has
no effect in MINIO mode).

> **⚠️ The bwrap path is unverified in production.** Our own dev / paper
> environment runs EvoAgent inside a Docker container whose default
> seccomp profile blocks `unshare()`, which bwrap depends on, so bwrap has
> never been observed working end-to-end in the original deployment.
> Treat it as reference code, not a production safeguard — validate on
> your own host before trusting it.

**What this does NOT protect against**

- **VM-grade isolation**: bwrap is host-level. A Linux kernel escape
  compromises the *server* host (which also holds your LLM keys and the
  whole `data/workspace/`).
- **seccomp-bpf**: we do not filter syscalls.
- **`sys.prefix` leakage**: the ro-bound `sys.prefix` contains whatever
  the server env has installed; if deployed via `pip install -e .`,
  EvoAgent's own code is reachable via `import core.agent`. Deploy with
  `python -m app.api.server` from the repo root to minimise this.
- **Non-Linux hosts** (macOS, Windows, BSDs): bwrap is unavailable and the
  backend silently falls back to `subprocess.run` with CWD confinement
  only.
- **`EXECUTE_CODE_SANDBOX=off`**: subprocess-only, no isolation at all.
  Single-tenant trusted use exclusively.

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
