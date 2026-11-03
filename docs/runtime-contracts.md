# Questions, delegation sources, tool outcomes and measured usage

## A question ends its own run

Main and child SDK agents share a tool-result callback for `ask_user_question`.
After a valid question result, the SDK finishes the current tool batch and returns
without another model request. Invalid arguments remain correctable by the model.
Already dispatched parallel tools retain their real results. Asking does not
undo their work or block another agent's independent tasks.

Each question result contains a `question_id`. A delegated question also travels
with its owner and task ID. The UI attaches this identity to an option selection
or free-text answer:

```json
{
  "userId": "alice",
  "sessionId": "research",
  "answerTo": {
    "question_id": "<question ID from the card>",
    "agent_type": "worker",
    "task_id": "report-a"
  },
  "data": {"input": "PDF"}
}
```

The server resolves the question against saved history. Submitted task/owner
values must agree with that record; they cannot select an arbitrary worker.
A pending child answer reuses its original task thread before skill matching
or a parent model call. A direct question continues the current agent. Answered,
unknown and mismatched identities produce a visible error without starting work.
Legacy text replies remain supported through ordinary model-directed handling.
The model and summary input retain the answered question's ID and text, while
the user's saved/displayed answer remains unchanged. An answer to a local
question continues that task without repeating trigger-based skill matching.

Answer metadata lives on the user history message. Compression carries pending
questions in deterministic metadata and its recovery index, so IDs do not depend
on an LLM reproducing them. Answered questions are not resurrected from an older
waiting receipt. New waiting or delivered receipts supersede older question
cards for that same worker/task. Question cards are separate; there is no merged questionnaire
or new task scheduler.

Implementation: [questions](../core/runtime/questions.py), [question tool](../core/plugins/ask_user.py)
and [chat UI](../app/static/index.html).

## Delegation receives a source snapshot

The main agent captures these inputs before starting the turn's execution:

| Section | Meaning |
|---|---|
| `current_user_input` | This turn's original input, including inline file text when supplied |
| `current_attachments` | Copies of this turn's image and file metadata/content |
| `prior_assets` | Up to 100 typed resource references from earlier history |
| Parent model notes | The model-written `context`, kept separate from source material |

Both explicit delegation and skill handoff use this same snapshot. Source data
is added to the worker's user-role input, rather than promoted into system
instructions. The worker follows its assigned task and keeps current uploads
distinct from historical outputs. A prior generated file is not proof that the
current task has already been completed.

`parent_history` remains available for additional detail. The snapshot does not
copy the entire parent conversation or include business-specific data parsers.
It is separate from the durable memory system.

Implementation: [delegation context](../core/runtime/delegation_context.py).

## Tool results and delivery facts

The adapter preserves successful legacy text and serializes dictionary results
as JSON. Explicit `ToolResult.error` is retained, including when `output` is
empty. Built-in search, fetch and image failures return an explicit error
envelope. Runtime handling does not guess success from English error wording.

Before display truncation, a tool result is projected into:

- `status`: `success`, `partial`, `error` or `unknown`;
- `is_error` and an optional `error`;
- `artifacts`: typed output references with their original URL and field.

Unstructured legacy text has `status=unknown`. Structured failure fields establish
an error. Generated references come from explicit output fields (`image_path`,
`file_url`, `download_url`, and similar fields) or output lists. A link in prose,
a source URL or a tool input is not automatically a generated artifact.

Delegation receipts retain individual `tool_outcomes` and artifact provenance.
Artifacts have `delivery_status=produced` until their URL appears in a visible
body/preview event; delivered ones also appear in `artifact_refs`. Tool-result
cards alone do not establish delivery. Intermediate files are not automatically
delivered, and there is no separate `deliver_artifacts` tool.

`execution_status=completed` means a tool invocation returned; it is distinct
from business success. A receipt's `delivery_complete` describes visible delivery,
not proof that all calls succeeded. Individual failed attempts remain visible
even if a later call succeeds; same-name calls are not assumed to be retries.
Fatal runner errors still produce failed receipts.

Implementation: [tool results](../utils/tool_results.py), [SDK events](../core/runtime/events.py)
and [delegation receipts](../core/delegation.py).

## Measured usage

Every main or direct child turn starts an independent collector. Delegated
workers explicitly inherit their parent's collector. SDK responses are
deduplicated by run and response position, including after compaction.
Reused or absent provider response IDs do not collapse separate requests.
Provider model names are retained when available.

Metrics expose `usage`, `usage_by_model` and `usage_by_category`: request counts,
input/output/total tokens, cached input tokens and reasoning tokens when the
provider reports them. Main, child, compression, embedding, skill-matching and
vision calls have separate categories. Standalone session reviews use a separate
scope and report their own usage.

`context_tokens` is the current history estimate; the old `tokens` field remains
as a compatibility estimate. Neither measures billed usage. Missing provider
usage is not synthesized, and fixed-price image/service costs are not estimated.
The former unused `needs_followup` completion-check model call has been removed.

Implementation: [usage data](../utils/usage.py), [SDK usage scope](../core/runtime/usage.py)
and [SDK run composition](../core/runtime/execution.py).

## Verification and boundaries

Offline tests run the installed SDK with scripted models and local workspaces:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p asyncio \
  tests/unit/test_runtime_contracts.py tests/unit/test_skill_evolution.py \
  tests/unit/test_usage.py -q
```

Tests exercise run boundaries, independent parent work, saved answer routing,
frontend answer identity, source snapshots, typed artifacts, explicit errors,
update confirmation and measured parent/child usage. Live model behavior and
cloud performance require separate evaluation. These components add no session
execution locks, distributed ownership or automatic tool retries.
