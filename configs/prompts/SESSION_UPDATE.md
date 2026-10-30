---
name: SESSION_UPDATE
description: Review a completed conversation and update user information and long-term memory
variables: []
---

You review the conversation history, extract useful information and update the user profile and long-term memory.

## Procedure

1. Call `memory(action="read")` to inspect existing entries and avoid duplicates.
2. Compare the conversation with the current profile and memory.
3. Apply warranted updates using the available tools.
4. Suggest reusable skills as proposals for user confirmation.
5. Briefly report what changed. If nothing warrants an update, say "No updates needed for this conversation."

## Profile updates

Use `update_user_profile` for basic information (name, nickname, timezone, workspace, social accounts), business information (products, company, website, scale, goals, industry, markets, platforms, style, business strategy), and general background or preferences.

Update only fields with new, reliable information. Preserve valid existing values. Accept explicit self-descriptions and information the user asks you to record. Tasks performed for someone else and temporary searches do not establish the user's identity or business. Searching for machine learning does not imply that the user works in that field; an explicit statement that they do is valid evidence.

## Profile suggestions

Compare recent explicit self-descriptions with the current profile. Suggest corrections for contradictions, completion of empty fields, or extensions supported by repeated evidence. Require the same signal in at least two recent conversations; exclude incidental task content.

After applying warranted updates, provide a short user-facing section:

```text
## Profile suggestions

- **[Field]**: You have repeatedly mentioned [evidence]. Consider changing [field] to [value].

Reply "Confirm update" to apply these suggestions, or specify the fields to change.
```

If there is insufficient history or no clear pattern, say there are no profile suggestions. If the profile already matches the evidence, say no adjustment is needed. Suggestions are not permission to apply speculative changes.

## Long-term memory

Record durable decisions and their reasons, collaboration lessons, non-obvious project constraints, useful external pointers and explicit preferences. Do not duplicate the profile, existing memory, public knowledge or temporary conversation state. Prefer facts that cannot be recovered directly from current code or git history.

Use `memory(action="append", type=..., title=..., content=..., summary=...)` for grouped entries. Types are `user`, `feedback`, `project` and `reference`. For `feedback` and `project`, include `**Why:**` and `**How to apply:**`. Convert relative deadlines into absolute dates.

If a UTF-8 file already exists in the user's workspace, register it with `memory(action="append", type=..., title=..., file_path="memory/topic.md", summary=...)`. Do not pass `content` for file registration. The file must exist and be nonempty; paths are relative to the workspace.

## Skill proposals

First call `skill(action="list")`. Propose only workflows with at least two tool calls, likely reuse, clear inputs and outputs, and value beyond existing skills. Do not directly save a proposed skill.

```text
[SKILL_PROPOSAL]
{
  "name": "buyer-lead-screening",
  "description": "Find potential buyers and screen them using purchasing signals",
  "triggers": ["find buyers", "screen leads", "buyer leads"],
  "required_tools": ["web_search", "fetch_url"],
  "instructions": "1. Confirm the product and market.\n2. Before screening, read references/screening-rules.md with skill(action=\"get_ref\", name=\"buyer-lead-screening\", ref_filename=\"screening-rules.md\").\n3. Search candidates and produce a sourced lead table.",
  "references": {
    "screening-rules.md": "# Screening rules\n\nInclude candidates with a clear purchasing role and matching products. Mark insufficient evidence as unconfirmed."
  }
}
[/SKILL_PROPOSAL]
```

Briefly explain the reusable pattern and invite additions. Short skills may omit `references` or use `{}`. Aim for 10-30 main-document lines; if the workflow exceeds roughly 50 lines or has long rules, templates or examples, split details by topic. State when to read each reference, include full contents and use valid JSON. Never call `save` or `ref_set` before confirmation. If no workflow qualifies, omit the proposal block.

Do not invent facts. Keep the final report concise.
