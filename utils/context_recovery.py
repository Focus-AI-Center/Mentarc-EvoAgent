"""Bounded, exact context references and lossless summary input segmentation."""

import json
import re

from utils.token_counter import count_tokens
from utils.tool_results import tool_observation

ASSET_MARK = "[Session asset index]"
CONTINUATION_NOTE = (
    "[History continuation] The summary records earlier conversation, not a new user request. "
    "Continue unfinished work using later user messages. Do not repeat delivered output or rerun completed actions."
)
_RESULT_FIELDS = ("details_url", "file_url", "download_url", "artifact_url", "image_path")
_URL_RE = re.compile(r"(?:https?|ffs)://[^\s<>`\"\)\]]+")


def _url(value):
    return isinstance(value, str) and value.startswith(("https://", "http://", "ffs://"))


def collect_assets(messages):
    """Read explicit envelopes; nested source URLs are not promoted to results."""
    refs = {}

    def add(kind, url, tool="", field=""):
        if not _url(url):
            return
        prior = refs.pop(url, {})
        if {"source": 0, "upload": 1, "result": 2}.get(prior.get("kind"), -1) > {
            "source": 0,
            "upload": 1,
            "result": 2,
        }.get(kind, 0):
            kind, tool, field = prior["kind"], prior.get("tool", ""), prior.get("field", "")
        refs[url] = {"kind": kind, "url": url, "tool": tool, "field": field}

    for message in messages:
        for ref in message.get("context_assets", []):
            if isinstance(ref, dict):
                add(ref.get("kind", "source"), ref.get("url"), ref.get("tool", ""), ref.get("field", ""))
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if ASSET_MARK in content and not message.get("context_assets"):
            for line in content.split(ASSET_MARK, 1)[1].splitlines():
                kind = "result" if line.startswith("- Generated") else "source"
                for url in _URL_RE.findall(line):
                    add(kind, url)
        if message.get("role") == "user" and not content.lstrip().startswith("<summary>"):
            for url in _URL_RE.findall(content):
                add("source", url)
            attachments = message.get("attachments") or {}
            images = attachments.get("images")
            if images is None and "[User-uploaded images]" in content:
                block = content.split("[User-uploaded images]", 1)[1].split("[User-uploaded files]", 1)[0]
                images = [
                    line.strip()
                    for line in block.splitlines()
                    if not any(char.isspace() for char in line.strip())
                ]
            for url in images or []:
                add("upload", url, field="image")
            files = attachments.get("files")
            if files is None and "[User-uploaded files]" in content:
                try:
                    files = json.loads(content.split("[User-uploaded files]", 1)[1])
                except (ValueError, TypeError):
                    files = []
            for item in files if isinstance(files, list) else []:
                if isinstance(item, dict):
                    for field in ("url", "file_url", "download_url"):
                        add("upload", item.get(field), field="file")
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            if function.get("name") not in ("fetch_url", "fetchurl"):
                continue
            try:
                args = json.loads(function.get("arguments", "{}"))
            except (ValueError, TypeError):
                continue
            if isinstance(args, dict):
                add("source", args.get("url"), function["name"], "url")
        if message.get("role") != "tool":
            continue
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if tool_observation(data)["status"] in ("error", "unknown"):
            continue
        tool = message.get("name") or "unknown"
        for field in _RESULT_FIELDS:
            add("result", data.get(field), tool, field)
        for container in ("files", "images", "artifact_refs"):
            items = data.get(container)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, str):
                    add("result", item, tool, container)
                elif isinstance(item, dict):
                    for field in ("url", "file_url", "download_url", "image_path"):
                        add("result", item.get(field), tool, f"{container}.{field}")
    return list(refs.values())


def render_index(messages, receipts, reloads, budget, *, questions=None):
    """Keep recent explicit outputs first, fit complete entries and never cut URLs."""
    parts = [ASSET_MARK]
    if questions:
        parts.append("- Pending user questions (resume the saved question and task identity):")
        parts.extend("  " + json.dumps(row, ensure_ascii=False) for row in questions)
    if reloads:
        parts.append("- Skill guides were unloaded; reload originals before executing their workflows:")
        for row in reloads:
            parts.append("  " + json.dumps(row, ensure_ascii=False))
    if receipts:
        parts.append("- Recent delegation receipts (reuse owner/task_id for revisions):")
        for receipt in receipts:
            compact = {
                key: value
                for key, value in receipt.items()
                if key
                not in (
                    "summary",
                    "agent_instruction",
                    "artifact_refs",
                    "delivery_id",
                    "session_id",
                    "delivered_codes",
                    "artifacts",
                    "tool_outcomes",
                )
                and (questions is None or key != "pending_questions")
            }
            parts.append("  " + json.dumps(compact, ensure_ascii=False))
    text = "\n".join(parts)
    if len(text) > budget:
        raise ValueError("task and skill recovery references exceed asset index budget")
    assets = collect_assets(messages)
    ranked = sorted(
        enumerate(assets),
        key=lambda pair: (
            {"result": 0, "upload": 1, "source": 2}.get(pair[1]["kind"], 2),
            -pair[0],
        ),
    )
    # The most recent result and upload must remain recoverable when present.
    required = {
        next((i for i in range(len(assets) - 1, -1, -1) if assets[i]["kind"] == kind), -1)
        for kind in ("result", "upload")
    }
    ranked.sort(
        key=lambda pair: (
            pair[0] not in required,
            {"result": 0, "upload": 1, "source": 2}.get(pair[1]["kind"], 2),
            -pair[0],
        )
    )
    selected = []
    for index, ref in ranked:
        line = "\n- " + json.dumps(ref, ensure_ascii=False)
        if len(text) + len(line) <= budget and len(selected) < 100:
            text += line
            selected.append((index, ref))
        elif index in required:
            raise ValueError("a required recovery URL cannot fit the asset index budget")
    return (text if len(parts) > 1 or selected else ""), [ref for _, ref in sorted(selected)]


def split_transcript(text, budget):
    """Split by character offsets measured in tokens; concatenate to recover input."""
    if budget < 1:
        raise ValueError("summary transcript budget must be positive")
    chunks, start = [], 0
    while start < len(text):
        low, high = start, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if count_tokens(text[start:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        end = low
        if end <= start:
            raise ValueError("a character exceeds the summary token budget")
        if end < len(text):
            boundaries = list(re.finditer(r"\n|[.!?](?=\s)", text[start:end]))
            if boundaries and boundaries[-1].end() > (end - start) // 2:
                candidate = start + boundaries[-1].end()
                if count_tokens(text[start:candidate]) <= budget:
                    end = candidate
        chunks.append(text[start:end])
        start = end
    return chunks
