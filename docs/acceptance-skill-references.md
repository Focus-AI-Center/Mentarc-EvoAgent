# Skill references and regression acceptance

Date: 2026-10-30.

Local acceptance passed after fixing the three reported defects and converting repository-owned documentation, prompts, tool messages and interface text to English. External-service verification boundaries are listed below.

## Fixed defects

1. **Concurrent memory index overwrites.** Memory writes use conditional updates with conflict retries. Local files use process-shared locks and unique temporary files; MinIO sends conditional headers in the PUT. Duplicate checks run against the latest index, and a competing grouped-file change is rejected during rewrite. Tests force simultaneous stale reads across independent Agent instances and exercise conditional writes from separate processes.
2. **Incorrect skill resource paths.** Template synchronization preserves `scripts/`, `assets/`, `references/` and other relative directories, including binary files. A layout-version change refreshes existing workspaces. Snapshots include auxiliary resources and keep nested skill resources separate. Tests execute the synced script with the documented sandbox cwd and restore binary assets from a snapshot.
3. **MCP discovery failures cached as empty results.** Consumers propagate catalog failures. The manager caches successful catalogs, including empty catalogs, and tracks failed servers separately. Later searches retry failed services while retaining available tools. Tests cover partial outage, recovery in the same run, registry-read failure and sandbox stdio failure.

Memory operations protect individual prompt updates; file creation, grouped body writes and index writes remain separate operations. See [memory recovery boundaries](memory.md#concurrent-updates-and-recovery).

## Automated checks

- Unit suite: **155 passed, 19 skipped**.
- The 19 skipped cases require a real MinIO test service, which was not configured.
- Conditional MinIO requests are covered using the installed SDK with a mocked HTTP transport; this does not replace a real-server integration run.
- Changed Python files pass lint and format checks; `git diff --check` passes.
- Inline JavaScript syntax, renamed resource links, documentation links and proposal JSON examples pass checks.
- The repository policy check finds **zero Han text occurrences** in existing tracked files and nonignored new files, including resource filenames.

Tests disable `.env` loading and use isolated local workspaces.

English-profile regressions also cover value preservation, complete legacy-label migration, rejection of incomplete legacy schemas, user-supplied Unicode data, frontend parsing and the profile API. Missing awaiting in the profile update route and incorrect multiline-field matching were fixed so these operations complete and preserve previous values.

## Browser acceptance

The browser harness uses the real `index.html`, actual Skill/Workspace routes, local storage and fixed model SSE fixtures. Markdown and sanitization libraries are the actual page dependencies. External images and fonts are outside the harness scope.

| Scenario | Required behavior |
|---|---|
| Session update produces a multifile proposal | Preview main text and two references; no writes before confirmation |
| Long reference preview and confirmation | Preserve the full Unicode/emoji body, source and origin session |
| Lazy reference reads | `get` lists references without embedding them; `get_ref` returns full text |
| Ordinary chat receives a child proposal | `code=12` carries the complete proposal beyond the ordinary summary cap |
| Legacy single-file proposal | Parse and save without references |
| Skip | Close the card without creating files |
| Truncated proposal | Show a retry notice and avoid publishing partial content |
| Invalid path | Return HTTP 422 without publishing the main text or references |

Local harness artifacts live under `/tmp/mentarc-skill-acceptance/`, with isolated generated workspaces. They do not write to real user data.

The scenarios above are grouped into **six Chromium acceptance cases**, all passing with **zero JavaScript runtime errors**. The long-reference case preserves 16,106 characters, including Unicode and emoji. Its SHA-256 remains:

```text
78fa640fe7a1c09297ea34607795d0519bc92ee163c6cf2b287983ead631efff
```

## Cloud Agent documentation

[README](../README.md), [the cloud runtime guide](cloud-agent.md), [skill extraction](skill-extraction.md), [memory](memory.md), [.env.example](../.env.example) and [deployment boundaries](../SECURITY.md) now describe direct MinIO text access separately from sandbox execution. They document session-container reuse, shared stdio MCP processes, mounted paths, cleanup, effective limits and API-process ownership. The direct-access latency rationale is architectural; no benchmark ratio is claimed.

## Verification boundaries

Paid live-model extraction quality, a real external MCP service and real MinIO persistence require their respective services. Fixed SSE tests verify transport, review, save and read behavior after proposal generation. The extraction prompt guides topic-based splitting; the model decides whether and how to split a long workflow.

Repository-owned source, prompts, examples, documentation, interface text and resource filenames use English. Unicode test data still exercises byte boundaries and non-ASCII round trips.
