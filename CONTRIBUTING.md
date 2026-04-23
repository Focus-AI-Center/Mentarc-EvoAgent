# Contributing to EvoAgent

Thanks for your interest. EvoAgent is a research artifact released alongside the paper — it is not a production-ready library — but patches, bug reports, and extensions are welcome.

## Before you file an issue

- Check [SECURITY.md](SECURITY.md) for known-unsafe components (`execute_code`, demo auth) before reporting "this is exposed to X" — some of those are intentional and documented.
- Skim open issues first.

## Dev setup

```bash
git clone https://github.com/Focus-AI-Center/Mentarc-EvoAgent
cd Mentarc-EvoAgent

python -m venv .venv
source .venv/bin/activate          # or ".venv\Scripts\activate" on Windows

pip install -e .[dev]
cp .env.example .env               # fill in at least OPENAI_API_KEY
```

Run the server:

```bash
python -m app.api.server           # or: evoagent-server
```

Run the CLI:

```bash
python cli.py --user demo          # or: evoagent-cli --user demo
```

## Project layout (the quick version)

| Where | What |
|---|---|
| `core/agent.py` | Main orchestrator (`EvoAgent`). ReAct loop, skill matching, Phase 2.5 trigger-based handoff. |
| `core/sub_agent.py` | `SubAgentRunner` — loads `configs/agents/*.md` into SDK `Agent`. |
| `core/skills/` | 3-stage skill matcher + skill manager. |
| `core/plugins/` | External tools (web_search, fetch_url, execute_code, ...) + internal tools (update_user_profile, update_memory, skill_manager, sub_agent, parent_history). |
| `configs/prompts/` | SOUL.md / USER.md / MEMORY.md / SESSION_UPDATE.md / SKILL_EXTRACT.md / SKILL_MATCH.md templates. |
| `configs/skills/` | Built-in skills, each in `{name}/SKILL.md` + optional `references/`. |
| `configs/agents/` | Sub-agent configs, each a single `.md` with YAML frontmatter. |
| `utils/workspace.py` | Per-user local-filesystem workspace. |
| `utils/history_compressor.py` | Nine-section history compaction. |

## Adding a new external plugin

1. Create `core/plugins/your_tool.py` following the shape of e.g. `core/plugins/web_search.py`:
   ```python
   class YourToolParams(BaseModel):
       query: str = Field(description="...")

   @register_plugin(PluginMeta(name="your_tool", category="...", ...))
   class YourTool(BaseTool):
       name: str = "your_tool"              # snake_case, must equal PluginMeta.name
       description: str = "..."             # shown to the LLM
       param_anno = YourToolParams

       async def __call__(self, params: YourToolParams) -> str:
           ...
   ```
2. Import and register in `core/plugins/__init__.py`:
   - Add to the top-level `from core.plugins.your_tool import ...` block
   - Add `"your_tool": YourTool` to `PLUGIN_CLASSES`
3. If the tool needs to be visible to the main agent by default, add a row to `configs/prompts/SOUL.md`'s tool table.

Naming rule (enforced everywhere): `PluginMeta.name == Tool.name == PLUGIN_CLASSES key == snake_case`.

## Adding a sub-agent

Drop a new file under `configs/agents/`, e.g. `configs/agents/report-writer.md`:

```markdown
---
name: report-writer           # kebab-case, matches filename
description: ...              # shown to the main agent via SubAgentTool
triggers:
  - 写报告
  - draft report
required_tools: []
skills: []
model: ""                     # "" = inherit global MODEL
max_steps: 12
timeout: 180
---

You are the report writer ...
```

Every sub-agent automatically gets the default tool set (see `DEFAULT_SUB_AGENT_TOOLS` + internal tools in `core/sub_agent.py::SubAgentRunner._build_tools`). If spawned from a parent agent, the `parent_history` tool is also available for on-demand context retrieval.

## Adding a skill

Create `configs/skills/your-skill/SKILL.md`:

```markdown
---
name: your-skill
description: ...
triggers: [...]
required_tools: [web_search]
---

1. Step one
2. Step two
```

Put supporting material in `configs/skills/your-skill/references/*.md`. The agent will load references on demand via `skill_manager(action="get_ref", ...)`.

## Code style

- Python ≥ 3.10.
- `ruff check .` and `ruff format --check .` must pass (run locally or watch CI).
- Keep tool names `snake_case`, agent / skill names `kebab-case`.
- Don't add unused imports; ruff will fail the build if you do.
- Docstrings only when the *why* is non-obvious — don't paraphrase the code.

## PR expectations

- One feature per PR. Keep diffs small and focused.
- Update `README.md` / `SOUL.md` / relevant `.md` if you change user-visible behaviour or tool naming.
- If you touch `execute_code` / auth / CORS / rate-limiting, update [SECURITY.md](SECURITY.md) accordingly.
- CI is lint-only today; if a PR introduces tests, please add a `tests/` directory and tell us in the PR description.

## Licence

By submitting a patch you agree that your contribution is released under the repository's [Apache-2.0 licence](LICENSE).
