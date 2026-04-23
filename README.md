# EvoAgent

[![arXiv](https://img.shields.io/badge/arXiv-2604.20133-b31b1b.svg)](https://arxiv.org/abs/2604.20133)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Official implementation of **"EvoAgent: An Evolvable Agent Framework with Skill Learning and Multi-Agent Delegation"** ([arXiv:2604.20133](https://arxiv.org/abs/2604.20133)).

[Reproducibility](REPRODUCIBILITY.md) · [Citation](#citation) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

> **⚠️ Pre-release preview.** The paper is available on [arXiv:2604.20133](https://arxiv.org/abs/2604.20133); the detailed paper-to-code mapping will be attached in the upcoming official release. Author list in `CITATION.cff` is final.

> **📦 Scope of this release.** This repository is **not the complete production codebase** behind the paper. It has been scrubbed for open-sourcing: internal
> business logic, proprietary prompts, customer-specific skills, and integrations with closed-source internal services have been removed or replaced with
> generic reference implementations. The released code preserves the framework's architecture (orchestrator · 3-stage skill matcher · sub-agent runner ·
> history compressor · 4-type memory · offline evolution loop) and is sufficient to reproduce the methodology; absolute numbers from the paper's foreign-trade
> deployment, however, require the internal corpus and skill library and cannot be reproduced from this repo alone.

> **🧨 Runtime safety notice.** The built-in tools that touch the filesystem
> and the shell — `execute_code` (bash / Python), `skill_manager` reference
> writes, and the per-user `workspace` directory — currently run **directly
> inside the server process** with only CWD confinement and a small regex
> blacklist. This is acceptable for a single-tenant research deployment and
> is what the paper's evaluations were run under, **but it is not safe to
> expose to untrusted users or the public internet as-is**.
>
> **About the shipped bubblewrap integration.** `core/plugins/execute_code.py`
> contains a `bwrap` wrapper path (auto-activates on Linux when `bwrap` is
> installed) that bind-mounts the user workspace into a per-invocation
> sandbox and applies `prlimit`. **This path has not been field-tested in a
> real production deployment** — we develop / run the paper inside a Docker
> container whose default seccomp profile blocks the `unshare()` syscall
> bwrap needs, so bwrap has effectively been disabled (`.env`
> `EXECUTE_CODE_SANDBOX=off`) in our own environment. The code is provided
> as a starting point; validate it on your own host before relying on it.
>
> **For any deployment beyond single-tenant trusted research, use a remote
> cloud sandbox instead.** Services like [E2B](https://e2b.dev),
> [Modal](https://modal.com), [Daytona](https://daytona.io), or a managed
> gVisor / Firecracker runtime offload the execution entirely and have been
> battle-tested at scale. Wire them into `execute_code` as a new backend
> (parallel to the current `bwrap` / `subprocess` branches) — the tool's
> JSON contract stays the same; only the transport changes.
>
> See [SECURITY.md §1](SECURITY.md) for the full caveat list and the
> `TODO(sandbox)` block at the top of
> [`core/plugins/execute_code.py`](core/plugins/execute_code.py).

---

## Overview

EvoAgent is an evolvable LLM-agent framework built around two coordinated loops:

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

**Offline evolution loop** — triggered after a session completes, the `session-reviewer` sub-agent consumes the transcript and emits three parallel updates: user profile (`USER.md`), long-term memory (`MEMORY.md` index + 4 typed detail files), and skill evolution (candidate extraction + maturity scoring). All writes land in the same workspace and are picked up on the next online turn.

![Offline Evolution Loop](docs/figures/offline-loop.svg)

The complete mapping between paper sections (methodology, appendix pseudocode) and source files will ship with the official release alongside the paper PDF.

## Quick Start

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

## Repository Layout

```
Mentarc-EvoAgent/
├── app/                    # API entry layer (FastAPI + SSE)
├── core/
│   ├── agent.py            # EvoAgent orchestrator (Section 3.3)
│   ├── agent_compat.py     # BaseTool -> OpenAI SDK FunctionTool adapter
│   ├── sub_agent.py        # Sub-agent runner (Appendix A Phase 2.5)
│   ├── plugins/            # External + internal tools
│   └── skills/             # Three-stage skill matcher + skill manager
├── configs/
│   ├── prompts/            # SOUL.md / USER.md / MEMORY.md templates
│   ├── skills/skill-creator# Built-in meta-skill for authoring new skills
│   └── agents/             # (Empty) drop-in sub-agent configs
├── utils/
│   ├── workspace.py        # Per-user local-filesystem persistence
│   ├── history_compressor.py  # Structured history compaction
│   ├── embedding.py
│   └── token_counter.py
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
Every interaction is written to `data/workspace/{user_id}/` on the server process's local disk. Nothing is sent to third parties except the LLM calls you have configured. `.gitignore` excludes `data/` and `logs/` — don't commit them.

**Is `execute_code` safe?**
No. It runs bash / Python in the user's workspace directory with a small regex blacklist and CWD confinement. See [SECURITY.md § 1](SECURITY.md) for the list of hardening steps required before exposing it. If you don't need it, remove `"execute_code"` from `PLUGIN_CLASSES` in `core/plugins/__init__.py` or set `DEFAULT_PLUGINS` to an explicit list that excludes it.

**Can I deploy this behind a public URL?**
Not as-is. At minimum: put it behind an authenticating reverse proxy, disable `execute_code`, add rate limiting, and review [SECURITY.md](SECURITY.md) in full. This is a research codebase.

**How do I add my own plugin / skill / sub-agent?**
See [CONTRIBUTING.md](CONTRIBUTING.md) — short sections on each.

**The agent keeps saying "Hi, I'm EvoAgent" — can I change that?**
Edit `configs/prompts/SOUL.md`. On first interaction it is copied into the user's workspace at `data/workspace/{user_id}/prompts/SOUL.md` and thereafter overwritten from source on every server restart (so edits to the source template propagate).

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
