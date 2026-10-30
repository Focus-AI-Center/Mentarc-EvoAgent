# EvoAgent

[![arXiv](https://img.shields.io/badge/arXiv-2604.20133-b31b1b.svg)](https://arxiv.org/abs/2604.20133)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Official implementation of **"EvoAgent: An Evolvable Agent Framework with Skill Learning and Multi-Agent Delegation"** ([arXiv:2604.20133](https://arxiv.org/abs/2604.20133)).

EvoAgent also provides a **Cloud Agent** architecture: the API process runs the
Agent, MinIO holds durable user workspaces, and a reusable session sandbox runs
code when needed. Skill and memory tools access storage directly; reading a
reference does not require an extra sandbox invocation. See
[Cloud Agent architecture and sandbox runtime](docs/cloud-agent.md).

[Reproducibility](REPRODUCIBILITY.md) · [Citation](#citation) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

> **⚠️ Pre-release preview.** The paper is available on [arXiv:2604.20133](https://arxiv.org/abs/2604.20133); the detailed paper-to-code mapping will be attached in the upcoming official release. Author list in `CITATION.cff` is final.

> **📦 Scope of this release.** This repository is **not the complete production codebase** behind the paper. It has been scrubbed for open-sourcing: internal
> business logic, proprietary prompts, customer-specific skills, and integrations with closed-source internal services have been removed or replaced with
> generic reference implementations. The released code preserves the framework's architecture (orchestrator · 3-stage skill matcher · sub-agent runner ·
> history compressor · 4-type memory · offline evolution loop) and is sufficient to reproduce the methodology; absolute numbers from the paper's foreign-trade
> deployment, however, require the internal corpus and skill library and cannot be reproduced from this repo alone.

> **🧨 Runtime safety notice.** The repo ships **two deployment modes**, picked
> at startup by whether `MINIO_ENDPOINT` is set:
>
> - **LOCAL mode** (default, no `MINIO_ENDPOINT`). Workspace data lives on the
>   server's local disk under `data/workspace/{user_id}/`. `execute_code`
>   runs as a plain `subprocess` scoped to the user's workspace directory
>   (see `core/plugins/sandbox/local.py`). **There is no kernel-level
>   isolation** — the dispatcher's regex blacklist, cwd confinement, and
>   timeout are the only barriers. Use this mode for single-machine research
>   / paper reproduction only.
>
> - **MINIO mode** (set `MINIO_ENDPOINT`). Workspace data lives in MinIO;
>   `execute_code` and stdio MCP share a Docker container per user/session,
>   optionally on a separate host through `DOCKER_HOST`. The user workspace is
>   mounted with s3fs at `/workspace`. Skill/reference operations use the server's
>   storage SDK directly. Containers, credentials and network policy still have
>   the deployment boundaries described in [SECURITY.md](SECURITY.md).
>
> Both modes still keep `execute_code` behind the same regex blacklist /
> timeout / output truncation; the difference is **where** the code runs
> and **what it can reach**. See [Deployment Modes](#deployment-modes) below
> for setup, and [SECURITY.md § 1](SECURITY.md) for the full caveat list.

---

## Overview

EvoAgent is an evolvable LLM-agent framework with a cloud workspace and an on-demand execution runtime, built around two coordinated loops:

- An **online execution loop** that runs user turns through a three-stage skill match, injects matched skills into the ReAct context, and compresses long histories on the fly.
- An **offline evolution loop** that reviews completed sessions, updates the user profile and long-term memory, extracts candidate new skills, and scores skill maturity.

The framework materialises the design philosophy of **Harness Engineering**: the reliability of an agent system is determined not only by the backbone model but also by the constraints, workflows, and feedback mechanisms around it. In a real-world foreign-trade deployment, integrating GPT-5.2 with EvoAgent improves a five-dimension LLM-as-Judge evaluation by ~28% on average over the same backbone model invoked directly (Section 4.1).

## Architecture

Following Figure 1 of the paper, EvoAgent is organised as five structural layers:

| Paper layer | Code entry point |
|---|---|
| API entry | `app/api/` |
| Orchestration | `core/agent.py::EvoAgent` |
| Runtime | OpenAI Agents SDK (`Runner.run_streamed`) via `core/agent_compat.py` |
| Tools + Session | `core/plugins/`, `utils/workspace.py` |
| Persistence | `utils/workspace.py`, `utils/history_compressor.py` |

The system runs two coordinated loops (Section 3 of the paper):

**Online execution loop** — every user turn loads profile/memory/skills from the per-user workspace, passes the query through a 3-stage skill matcher, injects matched `SKILL.md` into the ReAct context, then calls tools via the OpenAI Agents SDK. Long histories are compressed on the fly; complex turns may hand off to sub-agents.

![Online Execution Loop](docs/figures/online-loop.svg)

**Offline evolution loop** — users can request a session review through the memory-update button (`SessionUpdater`) or ask the main agent to delegate to `session-reviewer`. Reviews can update the user profile (`USER.md`), long-term memory (`MEMORY.md` plus registered files or typed detail files), and skill suggestions. Ordinary session completion saves history and does not automatically start a review. All writes land in the same workspace and are picked up on the next online turn.

![Offline Evolution Loop](docs/figures/offline-loop.svg)

The complete mapping between paper sections (methodology, appendix pseudocode) and source files will ship with the official release alongside the paper PDF.

### Cloud workspace and direct reference access

The workspace is durable Agent state, accessible independently of a running
sandbox. In MINIO mode, `skill` and `memory` call `Workspace` and
`MinIOSDKBackend` inside the API process. `execute_code` is the separate path
for computation: Docker exec runs code against the s3fs-mounted workspace.

```python
# Read stored reference text directly; no shell or Docker exec is needed.
skill(action="get_ref", name="buyer-lead-screening", ref_filename="rules.md")

# Use the execution runtime when the task actually needs a script.
execute_code(language="bash", code="python scripts/run.py",
             cwd="skills/buyer-lead-screening")
```

A bash read would add container lookup or startup, Docker exec, a shell and the
s3fs filesystem path to a simple storage operation. Direct MinIO reads and writes
avoid those execution costs, so `get_ref`, `save` and `ref_set` are the intended
fast path for skill documents. Warm sessions reuse their container, but an
extra exec is still unnecessary for ordinary reference access. This is an
architectural latency benefit, not a published benchmark.

Read the [Cloud Agent guide](docs/cloud-agent.md) for object-key/path mapping,
container lifecycle, shared stdio MCP processes, configuration and API-worker
ownership. See [skill extraction](docs/skill-extraction.md) for reference bundles.

### Memory index and skill directories

See [Memory storage and lifecycle](docs/memory.md) for the complete storage formats, read/write
flows, context limits, review triggers, backup behavior, and current limitations.

Long-term memory is scoped to a user workspace and shared across that user's
sessions. Files under `prompts/` have the following roles:

| File | Contents |
|---|---|
| `USER.md` | Structured user profile, loaded into context each turn. |
| `MEMORY.md` | Index: one line per memory, with type, title, date, and summary. |
| `MEMORY_user.md` | All user-background memories in one file. |
| `MEMORY_feedback.md` | All collaboration preferences and corrections in one file. |
| `MEMORY_project.md` | All project decisions, deadlines, and other project context in one file. |
| `MEMORY_reference.md` | All pointers to external information in one file. |

The four `MEMORY_{type}.md` files are the compatible grouped storage format.
You can also create independent UTF-8 memory files through `execute_code`, then
register each file in the index with the **`memory`** tool:

```python
execute_code(
    language="python",
    code='from pathlib import Path; Path("memory").mkdir(exist_ok=True); '
         'Path("memory/customer_acme.md").write_text("ACME purchasing preferences...", encoding="utf-8")',
)
memory(action="append", type="project", title="ACME customer background",
       file_path="memory/customer_acme.md", summary="Purchasing preferences and relationship context")
memory(action="get", title="ACME customer background")
```

The tool checks that the file exists and is nonempty, then adds only one index
line; it does not change the file. `file_path` and index links are relative to
the **user workspace root**, including when the index is under `prompts/`:

```markdown
- [project] [ACME customer background](memory/customer_acme.md) (2026-10-30) — Purchasing preferences
```

The `get` action resolves this link and reads the current file contents. Editing
the file through code does not require registering it again. Duplicate paths or
titles within the same type are not added again; missing files produce an error
without changing the index. The four types remain metadata and do not limit the
number of independent files. Workspace snapshots include registered memory files.

Omitting `file_path` and passing `content` to `append` retains the grouped format:
each detail file contains multiple entries separated by `## Title (YYYY-MM-DD)`
headings. The main agent and sub-agents automatically load only the first
**200 lines or 25KB (25,600 UTF-8 bytes)** of the index, whichever limit is reached
first. These fixed limits are defined in `utils/context_assets.py`; they are
independent of conversation-history compression settings.

Loading never deletes persisted entries. At 90% of either limit, agents receive
a maintenance reminder; above either limit, they are told that the remaining
index content is not automatically loaded. Memory reads and writes also report
index pressure. Use `memory(action="read")` to inspect the full index,
`get` with a title to retrieve one detail entry (up to 8,000 characters), and
`rewrite` with a type to merge or remove entries in that type's detail file.
Rewriting rebuilds grouped entries from all four detail files while preserving
registered file links; `rebuild_index` does the same and does not discover
unregistered files. Keep grouped entry titles and first body lines concise
because they become index titles and summaries. Update independent file bodies
through `execute_code`.

Longer memory bodies include the workspace-relative source path and a sandbox
bash read example. Use `execute_code` with `cat` or `sed` for additional content;
the memory tool does not expose pagination parameters. Code execution output
has its own size limit, so use `sed` to select the relevant lines when needed.

The skill management tool is named **`skill`**. Skill loading includes the skill's
workspace-relative directory, such as
`skills/your-skill/`, alongside its instructions. This applies to automatic
loading in the main agent and sub-agents, and to `skill(action="get")`.
Paths such as `scripts/` and `references/` in a skill are relative to that skill
directory. To run an existing script, use:

```python
execute_code(code="python scripts/run.py", cwd="skills/your-skill")
```

The relative `cwd` works in both execution modes. The directory resolves to
`{WORKSPACE_ROOT}/{user_id}/skills/your-skill/` locally and
`/workspace/skills/your-skill/` in the Docker sandbox. Loading instructions does
not execute a script; `execute_code` must be enabled to run one.

## Quick Start

Skill extraction supports a concise `SKILL.md` plus reference files for long
rules, templates, and examples. Users can inspect every file in the proposal
card and confirm the bundle once. See [Skill extraction and reference files](docs/skill-extraction.md).

Tool search is enabled by default for main agents and sub-agents. Resident
internal tools are available immediately; enabled plugin and MCP tool schemas
are loaded after `toolsearch(query="name or capability", limit=5)` finds them.
MCP catalogs are fetched on the first search, or earlier for explicitly required
or resident MCP tools. See
[Tool search](docs/tool-search.md) for configuration, lifecycle, and examples.

```bash
git clone https://github.com/Focus-AI-Center/Mentarc-EvoAgent
cd Mentarc-EvoAgent

conda create -n evoagent python=3.11 -y
conda activate evoagent
pip install -r requirements.txt

cp .env.example .env                 # fill in OPENAI_API_KEY, MODEL, etc.
python -m app.api.server             # launches FastAPI server on :8194
# or
python cli.py --user demo            # interactive CLI
```

The default `.env.example` leaves `MINIO_ENDPOINT` unset, which gives you the
**LOCAL** mode (workspaces in `data/workspace/`, `execute_code` via plain
subprocess). For multi-user / persistent / hardened deployments, switch to
**MINIO** mode — see the next section.

## Deployment Modes

| | **LOCAL** (default) | **MINIO** (Cloud Agent workspace) |
|---|---|---|
| Trigger | `MINIO_ENDPOINT` empty | `MINIO_ENDPOINT` set |
| Storage backend | `LocalFSBackend` (`data/workspace/{user_id}/`) | `MinIOSDKBackend` (S3 keys `{user_id}/...`) |
| Sandbox backend | `LocalSubprocessBackend` (plain subprocess) | `SessionDockerBackend` (reused container per user/session) |
| Workspace ↔ sandbox | direct file access on the same host | s3fs mount of the MinIO bucket inside the container |
| Durable state | Local disk; retain the workspace directory across restarts | MinIO objects, independent of API and execution containers |
| Live runtime ownership | Local subprocesses | API-process-owned container/MCP registries; coordinate workers before scaling |
| Cross-mode configs | rejected at startup (`make_sandbox_backend` / `get_backend`) | |

The current factory selects session Docker execution. The legacy one-shot
`RemoteDockerBackend` remains in the source but is not the default. See the
[cloud runtime guide](docs/cloud-agent.md#session-sandbox-lifecycle) for lifecycle
and [worker ownership](docs/cloud-agent.md#persistence-and-api-process-ownership)
before adding API replicas.

### Deploy MinIO

We do not bundle a MinIO deployment manifest. Provide a reachable MinIO service
and a bucket. The storage backend uses conditional PUT for concurrent memory
updates, so the deployed service must support `If-Match` / `If-None-Match`.
See the [cloud runtime guide](docs/cloud-agent.md) for the storage contract.

Once it's up, create a bucket (default name in `.env.example`:
`evoagent-workspaces`) and copy its endpoint URL + access/secret keys.
The server will create the per-user prefixes (`{user_id}/prompts/...`,
`/skills/`, `/sessions/`, `/images/`, `/previews/`) on first interaction.

### Deploy the remote sandbox host (MINIO mode only)

Any Linux box with Docker, `/dev/fuse`, and network access to the MinIO
endpoint. We've tested on Alibaba Cloud Linux 3 / kernel 5.10; any
distribution with kernel ≥ 4.18 and Docker ≥ 20.10 should work.

```bash
# 1. On the sandbox host: install Docker if needed
curl -fsSL https://get.docker.com | sh
docker --version                 # >= 20.10
ls -la /dev/fuse                 # must exist; needed for s3fs in container

# 2. Build the sandbox image (run from the repo root, on the sandbox host or
#    against the remote daemon via DOCKER_HOST)
cd deploy/sandbox-image
docker build -t evoagent-sandbox:python3.11 .

# 3. (Optional) verify the image can mount your bucket
docker run --rm --cap-add SYS_ADMIN --device /dev/fuse:/dev/fuse:rwm \
  --security-opt apparmor=unconfined --network host \
  -e MINIO_ENDPOINT=<minio-url> \
  -e MINIO_ACCESS_KEY=<key> -e MINIO_SECRET_KEY=<secret> \
  -e MINIO_BUCKET=<bucket> -e USER_ID=_smoke \
  -e LANGUAGE=bash -e TIMEOUT_S=10 \
  -e CODE='ls -la; echo from-sandbox > probe.txt && cat probe.txt' \
  evoagent-sandbox:python3.11
```

The build pre-installs the whitelisted scientific stack (`numpy`,
`pandas`, `requests`, `httpx`) plus s3fs-fuse. Final image is ~300 MB and
takes ~1 min cold (apt cached) on a clean daemon. From a region without
PyPI reachability, the bundled `Dockerfile` already routes pip through
the Aliyun mirror; swap to your own mirror by editing
[`deploy/sandbox-image/Dockerfile`](deploy/sandbox-image/Dockerfile).

### Wire the server to the sandbox host

The Python `docker` SDK reads `DOCKER_HOST` from the environment. Three
transports, in order of preference:

| `DOCKER_HOST` | When to use |
|---|---|
| *(empty)* | Server runs on the same box as the docker daemon (e.g. server + sandbox host co-located). Uses `/var/run/docker.sock`. |
| `ssh://user@host` | Server is separate from the sandbox host. **Requires SSH public-key auth** (the SDK does not accept passwords). Set up via `ssh-copy-id` + ensure the host is in the server's `~/.ssh/known_hosts`. |
| `tcp://host:2375` | Daemon is exposed over TCP. Insecure unless behind a VPN or fronted by TLS (`tcp://host:2376` + `DOCKER_TLS_VERIFY=1`). |

Concrete example for `ssh://`:

```bash
# On the server box (one-time):
ssh-keygen -t rsa                                       # if ~/.ssh/id_rsa missing
ssh-copy-id root@<sandbox-host>                         # push pubkey
ssh-keyscan -H <sandbox-host> >> ~/.ssh/known_hosts     # accept fingerprint
ssh -o BatchMode=yes root@<sandbox-host> 'docker version'  # must succeed without prompting

# In .env on the server:
MINIO_ENDPOINT=http://<minio-host>:9000
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_BUCKET=evoagent-workspaces
SANDBOX_BACKEND=docker
SANDBOX_IMAGE=evoagent-sandbox:python3.11
SANDBOX_NETWORK=host
DOCKER_HOST=ssh://root@<sandbox-host>
```

On startup the server runs a fail-fast configuration check
([`app/api/server.py`](app/api/server.py)): it instantiates both backends,
logs `Runtime mode: MINIO  storage=MinIOSDKBackend  sandbox=RemoteDockerBackend`,
and refuses to start on inconsistent combinations (e.g. `SANDBOX_BACKEND=docker`
but no `MINIO_ENDPOINT`).

### Verify end-to-end

```bash
# Set the single public application origin first.
: "${SERVER_BASE_URL:?Set the public application URL}"
# 1. /health
curl -s "${SERVER_BASE_URL%/}/health"
# {"status":"ok"}

# 2. Trigger an execute_code call and confirm the sandbox label is the docker one
curl -N -H 'Content-Type: application/json' "${SERVER_BASE_URL%/}/api/agent/chat" \
  -d '{"userId":"smoke","sessionId":"s1","plugins":["execute_code"],
       "data":{"input":"Run print(2+3) with execute_code"}}' \
  | grep -o '"sandbox": "[^"]*"' | head -1
# "sandbox": "docker(evoagent-sandbox:python3.11,net=host)"
```

A failure with `Cannot reach Docker daemon (...): Server '<host>' not found
in known_hosts` means `DOCKER_HOST=ssh://...` is set but key auth isn't
ready — re-run the `ssh-copy-id` / `ssh-keyscan` steps above.

## Repository Layout

```
Mentarc-EvoAgent/
├── app/                    # API entry layer (FastAPI + SSE)
├── core/
│   ├── agent.py            # EvoAgent orchestrator (Section 3.3)
│   ├── agent_compat.py     # BaseTool -> OpenAI SDK FunctionTool adapter
│   ├── sub_agent.py        # Sub-agent runner (Appendix A Phase 2.5)
│   ├── plugins/
│   │   ├── execute_code.py # Backend dispatcher (validation + sandbox.run)
│   │   └── sandbox/        # Pluggable sandbox backends
│   │       ├── local.py    # LocalSubprocessBackend (LOCAL mode)
│   │       ├── session_docker.py # SessionDockerBackend (MINIO mode)
│   │       └── remote_docker.py  # Legacy one-shot backend
│   └── skills/             # Three-stage skill matcher + skill manager
├── configs/
│   ├── prompts/            # SOUL.md / USER.md / MEMORY.md templates
│   ├── skills/skill-creator# Built-in meta-skill for authoring new skills
│   └── agents/             # (Empty) drop-in sub-agent configs
├── utils/
│   ├── workspace.py        # Per-user persistence (routes to backend below)
│   ├── context_assets.py   # Memory index limits and skill directory guidance
│   ├── storage_backend/    # Pluggable storage backends
│   │   ├── localfs.py      # LocalFSBackend (LOCAL mode)
│   │   └── minio_sdk.py    # MinIOSDKBackend (MINIO mode)
│   ├── history_compressor.py  # Structured history compaction
│   ├── embedding.py
│   └── token_counter.py
├── deploy/
│   └── sandbox-image/      # Dockerfile + entrypoint for evoagent-sandbox
├── evaluation/             # RQ1 / RQ2 / RQ3 experiment scripts
├── REPRODUCIBILITY.md
├── CITATION.cff
├── SECURITY.md
├── CONTRIBUTING.md
└── README.md
```

## Reproducing Paper Experiments

See [REPRODUCIBILITY.md](REPRODUCIBILITY.md). The end-to-end commands live under `evaluation/`:

```bash
# RQ1: backbone capability enhancement (baseline vs. EvoAgent, 5-dimension judge)
python evaluation/rq1_capability/run_eval.py --mode baseline  --test-set DATA --output ...
python evaluation/rq1_capability/run_eval.py --mode evoagent  --test-set DATA --output ...
python evaluation/rq1_capability/judge.py    --test-set DATA --baseline ...  --treatment ...

# RQ2: model-transferability study
MODEL=gpt-4.1        python evaluation/rq2_transferability/run_model_swap.py ...
MODEL=qwen-3.5-35b   python evaluation/rq2_transferability/run_model_swap.py ...

# RQ3: capability boundary / long-session stress test
python evaluation/rq3_boundary/long_session_stress_test.py --max-turns 420
```

## Test Dataset

The 172-sample foreign-trade corpus used in RQ1/RQ2/RQ3 (distilled from 664 multi-turn production dialogues) contains proprietary business data and is not bundled with this repository. A release that reproduces the exact numbers in the paper can be made available upon reasonable request for academic collaboration.

## FAQ

**Do I need every API key listed in `.env.example`?**
No. Only `OPENAI_API_KEY` is required. The others (Doubao, Tavily) only activate the matching plugin — leave them empty to disable that plugin. If `DEFAULT_PLUGINS=all`, unconfigured plugins will load but return a clear "API key not configured" error when the agent tries to call them.

**The agent says "Model not found" / 404 from my OpenAI endpoint.**
`MODEL`, `MODEL_VISION` and `COMPRESSION_MODEL` default to names from the paper's experimental setup. If your endpoint doesn't expose those names, override via env vars: `MODEL=gpt-4o`, `COMPRESSION_MODEL=gpt-4o-mini`, `MODEL_VISION=gpt-4o` are reasonable defaults on the public OpenAI API.

**What happens to my data?**
In LOCAL mode, every interaction is written to `data/workspace/{user_id}/` on the server process's local disk. In MINIO mode, the same per-user tree lives as keys under `{user_id}/...` in your MinIO bucket. Nothing is sent to third parties except the LLM calls you have configured. `.gitignore` excludes `data/` and `logs/` — don't commit them.

**Is `execute_code` safe?**
LOCAL execution is a subprocess under the server's user account, without
kernel isolation. MINIO execution uses a reusable Docker session container and
an s3fs workspace, optionally on a separate host. Containers share the configured
network mode and MinIO credential scope; the current backend does not implement
per-user STS credentials or per-call network isolation. Review
[SECURITY.md](SECURITY.md) and the [runtime details](docs/cloud-agent.md) for the
current boundaries. Exclude `execute_code` from `DEFAULT_PLUGINS` if it is not
needed.

**Can I deploy this behind a public URL?**
Not as-is. At minimum: put it behind an authenticating reverse proxy, disable `execute_code`, add rate limiting, and review [SECURITY.md](SECURITY.md) in full. This is a research codebase.

**How do I add my own plugin / skill / sub-agent?**
See [CONTRIBUTING.md](CONTRIBUTING.md) — short sections on each.

**The agent keeps saying "Hi, I'm EvoAgent" — can I change that?**
Edit `configs/prompts/SOUL.md`. On first interaction it is copied into the user's workspace at `data/workspace/{user_id}/prompts/SOUL.md` and refreshed when the platform template sync version changes (so source template edits propagate while user memory remains intact).

**Why does `ruff check .` pass but my IDE shows warnings?**
The ruleset in `pyproject.toml` is deliberately loose (E, F, I, UP, B only — no line-length enforcement, no Pylint-style nitpicks). Tighten locally if you like, but CI only enforces the shipped config.

## Citation

If you use EvoAgent in your research, please cite:

```bibtex
@misc{zhang2026evoagent,
  title         = {EvoAgent: An Evolvable Agent Framework with Skill Learning and Multi-Agent Delegation},
  author        = {Aimin Zhang and Jiajing Guo and Fuwei Jia and Chen Lv and Boyu Wang and Fangzheng Li},
  year          = {2026},
  eprint        = {2604.20133},
  archivePrefix = {arXiv},
  primaryClass  = {cs.CL},
  url           = {https://arxiv.org/abs/2604.20133},
  note          = {Aimin Zhang is the corresponding author.}
}
```

See [CITATION.cff](CITATION.cff) for the machine-readable citation record.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Public distribution

This release retains formal author names, copyright notices and official
repository links. Corporate author email addresses and deployment-specific
service details are omitted. This is an attributed release, not an anonymous one.

Configure one public application origin through `SERVER_BASE_URL`; serve the UI
and its `/api` routes from that same origin. The frontend uses same-origin URLs.
Set `ALLOWED_ORIGINS` for that origin when deploying. Local listener addresses
are development settings, not advertised service entrances.

Provider URLs are separate outbound configuration: `OPENAI_API_BASE`,
`OPENAI_API_BASE_SECONDARY`, `OPENAI_API_BASE_VISION`, `EMBEDDING_API_BASE`,
`DOUBAO_API_BASE` and `APIFY_API_BASE` include their API base paths.
`DOUBAO_GENERATIONS_URL` and `DOUBAO_EDITS_URL` accept complete gateway routes.
Set storage endpoints and scraper actor IDs through the environment.
Do not add deployment hostnames or credentials to the example file.

Before sharing source, run `python3 scripts/release_check.py --history --output /tmp/evoagent-source.tar.gz`.
It checks all reachable Git history, current tracked source and non-ignored new files, then packages only
that checked snapshot, excluding Git history and local state. Upload this archive,
not the working directory. The check detects known disclosure patterns and common
credential formats; review additional deployment-specific secrets before upload.
