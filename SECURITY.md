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
10,000-character output truncation per stream. Everything else depends on the backend.

#### MINIO mode -> `SessionDockerBackend`

The current factory reuses one Docker container per `(user_id, session_id)`.
`execute_code` calls and configured stdio MCP processes share that container.
The Docker daemon can run on a separate host through `DOCKER_HOST`; a separate
host is a deployment choice, not an automatic guarantee. The legacy one-shot
`RemoteDockerBackend` remains available in the source but is not selected by
this factory.

- **Container privileges.** The image uses `SYS_ADMIN`, `/dev/fuse` and
  `apparmor=unconfined` for s3fs. Commands run through Docker exec as the
  unprivileged `sandbox` user, but these settings are not VM-grade isolation.
- **Workspace view.** s3fs mounts `{bucket}:/{user_id}` at `/workspace`.
  Server-side MinIO SDK operations and mounted-file operations address the same
  object namespace. Prefix mounting limits the filesystem view; it does not
  establish object-store authorization. Filesystem metadata may be cached and
  arbitrary cross-file operations are not transactional.
- **Credentials.** The container receives the configured server MinIO keys.
  The entrypoint writes a root-owned credential file and clears key variables
  from its own environment before entering session mode. This does not create
  per-user STS credentials or establish a complete credential-isolation boundary;
  the Docker container configuration still contains the supplied environment.
- **Lifetime.** Containers are reused, not removed after every command. Idle
  cleanup runs every 60 seconds with a default 900-second TTL. Shutdown removes
  owned containers; startup orphan cleanup removes all session-labelled
  containers on the daemon. The registry is process-local, so uncoordinated API
  workers can interfere with each other's active sessions.
- **Resource limits.** A session memory limit is configured at container creation.
  Command timeouts and output caps apply to `execute_code`; they are not a hard
  lifetime cap for the entire session or its MCP processes. Commands may execute
  concurrently in the same container.
- **Network.** `SANDBOX_NETWORK` defaults to `host`. The current session backend
  does not enforce per-call network isolation from `EXECUTE_CODE_NETWORK=deny`.
  Actual egress control must be configured on the host/container network.

Before accepting untrusted workloads, use scoped storage credentials, enforce
network access, apply stronger runtime isolation and resource controls, and
coordinate session ownership. A separate sandbox host can separate compute from
the API host, but its reachability and credential scope still determine the
impact of a compromise. LOCAL mode offers less isolation, as described below.

See [Cloud Agent architecture and sandbox runtime](docs/cloud-agent.md) for
current paths, lifecycle, configuration and ownership details.

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
