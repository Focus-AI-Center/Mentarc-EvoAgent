# Context compression

For example, an agent reads a long document, compares several sources and then
loads another skill in the same user turn. The new tool results can exceed the
history watermark before the next user message arrives. Compression must retain
the user's constraints, known facts, completed actions and recoverable resources
so the agent can continue without repeating work.

## One path at turn and tool boundaries

Main and child agents use `HistoryCompressor.compress_history()` before execution
and through `CompactingAgent.get_all_tools()` before each SDK model request. The
SDK adapter waits for the previous parallel tool batch to complete, folds its
items into canonical history, and replaces the model input only after successful
compression. Completed SDK items represented in the new history are cleared
from the pending item list so they cannot be appended again.

This keeps one SDK run and its step budget. Already queued SSE events, raw model
responses and usage remain available. Tools are neither re-executed nor emitted
again. Persistence uses the compacted base plus subsequent SDK items; the main
and child entrypoints share the same adapter.

`MAX_HISTORY_TOKENS * COMPRESSION_THRESHOLD` is the history watermark. Counting
uses actual Unicode text without JSON ASCII escaping and treats literal
token-like strings as ordinary text. Display-only replay metadata is excluded.
This is a history budget, not a measurement of system instructions, tool schemas
or a guarantee that every provider's model window will fit.

## Summary fidelity and input budgets

The summary model receives complete chronological text, including tool names
and arguments. User messages and large tool results are not reduced to fixed
character prefixes or placeholders before summarization. Previous summaries are
marked as historical data, and their recovery index follows a separate
deterministic path instead of being recursively copied into prose.

If the transcript exceeds the summary input budget, it is split into contiguous
token-bounded fragments. Every character is assigned to a fragment. Message or
sentence boundaries are preferred; an exceptionally long span may continue in
the next fragment. Fragment instructions identify this possibility. Their
summaries are merged with the same fact-preservation rules, within a bounded
number of rounds. Prompts and framing count toward the input budget.

The output preserves intent and user corrections, object-specific facts and
values, evidence and uncertainty, decisions and failures, completed work,
remaining tasks and next actions. Reading, generating, validating and delivering
are distinct states. A structured summary still depends on the model's judgment;
complete input coverage does not guarantee perfect semantic retention.

Only a normally completed response containing one nonempty, closed
`<summary>...</summary>` block is accepted. Truncated, refused, empty or malformed
responses are rejected. Optional analysis blocks are removed and never saved;
reasoning-only output cannot substitute for a summary.

## Recent messages and tool pairs

Moderate compression retains up to the configured recent-message target, within
a separate tail token budget. A parallel tool group can increase the message
count to keep its calls and results together. If that whole group is too large,
it moves into the summarized prefix instead of retaining an unbounded current
user turn or truncating individual tool results. Incomplete tool-call groups are
folded into the summarized prefix. Aggressive compression retains no raw tail.

Light compression does not call the summary model: it unloads reloadable skill
documents and removes JSON formatting whitespace while preserving literal
numbers and string values. Large plain-text results remain intact. It can
therefore save less space than moderate or aggressive compression.

## Recovery references

A deterministic index preserves task identities, skill reload pointers and
typed resource references across compression:

- Task receipts retain the owner, task ID, status and pending questions. Long
  receipt prose and duplicate file lists do not consume the index budget.
- Pending question identities and answer routes follow a deterministic path.
  Answered questions are removed, including when an old waiting receipt remains
  in a summary. Questions that cannot fit the recovery budget prevent compression.
- Skill pointers contain the skill name and, when relevant, its reference path.
  They instruct the agent to reload originals and do not establish that a guide
  is currently loaded.
- Results come from explicit output fields such as `details_url`, `file_url`,
  `download_url`, `artifact_url`, `image_path`, `files`, `images` and
  `artifact_refs`. Arbitrary nested source links are not promoted to results.
- Uploads and source URLs remain distinct from output files. URL values are
  copied from the original records rather than reconstructed by the summary.

The index has a character budget and at most 100 selected resource references.
It fits complete entries, never sliced URLs. The newest explicit result and
latest upload receive fallback protection; other results, uploads and sources
are selected deterministically by type and recency. Older optional references
can be evicted. Selected records retain chronological order in metadata so a
second compression cannot reverse recency or resurrect discarded entries.

If required task/skill references or a protected URL cannot fit, compression
keeps the original history. This bounded deterministic selection does not
implement model-based reference ranking or archive discarded raw tool output.

## Skill lifecycle and failure handling

Original skill and reference documents are replaced by reload instructions
before any summary call. After actual replacement, the existing mounter removes
skill-introduced tools, revokes skill-specific grants and clears guide-loading
deduplication. The SDK then computes a fresh schema list. Resident tools and
independent search grants retain their own lifecycles.

After compression, a newly reloaded guide can temporarily exceed the soft
watermark while remaining below `MAX_HISTORY_TOKENS`. If the new SDK calls only
reload guides, the next model request is allowed to use them. This prevents a
load/unload loop. Guides exceeding the full configured history budget still
require smaller main documents and on-demand references.

One timeout covers all fragment and merge requests. Request failure, invalid
output, timeout, an unfit recovery index, or a summary that is not smaller keeps
the original history and does not unload tools. Failed expensive compression
attempts are not retried on every tool step within the same compressor instance;
a new request can try again. User cancellation propagates rather than becoming
an accepted summary. Successful guide-only unloading can avoid a model call.

These safeguards retain data on failure; they do not guarantee that the original
history will fit the next model request or recover execution after a restart.

## Configuration

| Setting | Default | Purpose |
|---|---:|---|
| `MAX_HISTORY_TOKENS` | 128000 | Configured history budget |
| `COMPRESSION_THRESHOLD` | 0.5 | Watermark fraction |
| `COMPRESSION_LEVEL` | moderate | none, light, moderate or aggressive |
| `MAX_HOT_TAIL_MESSAGES` | 4 | Recent-message target; zero disables the tail |
| `COMPRESSION_HOT_TAIL_TOKENS` | 6000 | Tail token budget |
| `COMPRESSION_MAX_INPUT_TOKENS` | 32000 | Summary request input, including instructions |
| `COMPRESSION_OUTPUT_TOKENS` | 8000 | Per-summary completion budget |
| `COMPRESSION_TIMEOUT_SECONDS` | 120 | Shared compression deadline |
| `COMPRESSION_ASSET_INDEX_CHARS` | 12000 | Recovery index character budget |

`MIN_MESSAGES_TO_KEEP` remains a legacy fallback when `MAX_HOT_TAIL_MESSAGES` is
unset. `COMPRESSION_MODEL` selects the summary model; configure input and output
budgets for that model's actual limits. Compression is separate from the durable
`MEMORY.md` index and its registered files.

## Offline verification

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p asyncio \
  tests/unit/test_history_compression.py tests/unit/test_sdk_compaction.py \
  tests/unit/test_skill_tool_lifecycle.py -q
```

The tests verify complete input coverage, Unicode/token handling, malformed and
truncated responses, deadlines and cancellation, nonshrinking summaries, tool
pair boundaries, exact resource recovery, repeated compression, fresh SDK tool
schemas, guide reloads, and persistence through the real main/child SDK loops.
They use scripted models and local storage without paid model calls. Summary
quality on real tasks requires separate model evaluation.
