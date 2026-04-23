# Security Policy

## Deployment posture

EvoAgent is a **research artifact**. It is designed for single-tenant, trusted-network use alongside the paper. The codebase has **not** been audited for production security and should not be exposed to untrusted users or the public internet without significant hardening.

Several components ship with explicit demo-level implementations that are called out below. Production operators are expected to replace them with real alternatives.

## Known limitations (do not deploy publicly without mitigating)

### 1. `execute_code` tool runs in a soft sandbox

[`core/plugins/execute_code.py`](core/plugins/execute_code.py) executes arbitrary bash / Python inside the user's workspace directory.

**Current safeguards**

- CWD confined to `WORKSPACE_ROOT/{user_id}/`
- Regex blacklist for a handful of destructive patterns (`rm -rf /`, `mkfs`, `dd if=`, `shutdown`, fork bombs, raw-device overwrite)
- 30s default timeout, 10 KB output truncation
- **Bubblewrap sandbox** (code path only, **not field-tested** — see below): `core/plugins/execute_code.py` contains a bwrap wrapper that auto-activates on Linux when `bwrap` is installed. Every invocation spawns an ephemeral sandbox that rw-binds only the user's own workspace directory, ro-binds `sys.prefix` + standard system paths, unshares user / ipc / pid / uts / cgroup namespaces, and enforces `RLIMIT_AS` (default 512 MB) + `RLIMIT_CPU` (= timeout) via `prlimit`. Network access is allowed by default; disable via `EXECUTE_CODE_NETWORK=deny`. Force or disable bwrap via `EXECUTE_CODE_SANDBOX=bubblewrap|off`.

> **⚠️ The bwrap path is unverified in production.** Our own dev / paper environment runs EvoAgent inside a Docker container whose default seccomp profile blocks `unshare()`, which bwrap depends on. The integration is shipped disabled (`EXECUTE_CODE_SANDBOX=off` in `.env`) and has never been observed working end-to-end in the original deployment. Treat it as reference code, not a production safeguard — validate on your own host before trusting it. For any deployment that accepts untrusted input, prefer a **remote cloud sandbox** (E2B, Modal, Daytona, a managed gVisor / Firecracker runtime) wired in as a new backend parallel to the current `bwrap`/`subprocess` branches in `execute_code.py`.

**What the sandbox does NOT protect against**

- **VM-grade isolation**: bwrap is host-level (mount + namespace), not a VM. A Linux kernel escape (rare but not zero) compromises the host. For adversarial multi-tenant use, upgrade to gVisor / Kata / Firecracker.
- **seccomp-bpf**: we do not yet filter syscalls. Dangerous syscalls reachable by the code still execute under the kernel.
- **`sys.prefix` leakage**: the ro-bound `sys.prefix` contains whatever the server env has installed, including — if you deployed via `pip install -e .` — EvoAgent's own code (reachable via `import core.agent`). This by itself doesn't let sandboxed code touch other users' workspaces (those paths aren't bound) but it does leak framework internals. Deploy with `python -m app.api.server` from the repo root instead of an editable install to avoid this.
- **Non-Linux hosts** (macOS, Windows, BSDs): bwrap is unavailable. The tool silently falls back to `subprocess.run` with CWD confinement only — exactly the behaviour before the sandbox patch. Do not expose an `execute_code`-enabled server on non-Linux.
- **Subprocess-without-bwrap** (`EXECUTE_CODE_SANDBOX=off` or fallback): the sandbox is off, back to pre-sandbox guarantees. Only safe for single-tenant trusted use.

**Before exposing `execute_code` to untrusted users**

1. Confirm `EXECUTE_CODE_SANDBOX=bubblewrap` resolves (not `off`), and the host has `bwrap` installed.
2. Consider `EXECUTE_CODE_NETWORK=deny` unless the workflow requires outbound net.
3. Add a seccomp-bpf syscall allow-list on top of bwrap, or upgrade to gVisor / Firecracker.
4. Add per-user concurrency + QPS caps (see also § 3).
5. Persist an audit log for every invocation (see `TODO(sandbox)` in `execute_code.py` for the full hardening ladder).

If your deployment does not need `execute_code`, remove it from [`core/plugins/__init__.py`](core/plugins/__init__.py)'s `PLUGIN_CLASSES` and from `DEFAULT_PLUGINS`.

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
