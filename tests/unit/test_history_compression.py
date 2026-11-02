"""Lossless summary inputs, bounded recovery and failure-safe replacement."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from utils.context_recovery import ASSET_MARK, collect_assets, render_index, split_transcript
from utils.history_compressor import CompressionUnavailable, HistoryCompressor
from utils.token_counter import count_tokens


def response(text="<summary>Continue with the remaining task.</summary>", reason="stop", **kwargs):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=reason,
                message=SimpleNamespace(content=text, refusal=None, **kwargs),
            )
        ]
    )


def compressor(create=None, **kwargs):
    create = create or AsyncMock(return_value=response())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return HistoryCompressor(max_history_tokens=10, min_messages_to_keep=0, async_client=client, **kwargs)


def large_history():
    return [
        {"role": "user", "content": "User requirements: " + "evidence " * 2500 + " NEVER DELETE THE FILE."}
    ]


@pytest.mark.asyncio
async def test_summary_reads_entire_history_and_tool_arguments_in_bounded_fragments():
    requests = []

    async def create(**kwargs):
        requests.append(kwargs["messages"])
        return response()

    comp = compressor(create, max_input_tokens=1800)
    user = "START " + "document paragraph.\n" * 1800 + "FINAL_USER_CORRECTION"
    tool_body = "BEGIN_RESULT " + "unique evidence.\n" * 1200 + "FINAL_RESULT_FACT"
    history = [
        {"role": "user", "content": user},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "work",
                    "function": {
                        "name": "business",
                        "arguments": json.dumps({"constraint": "EXACT_LATE_ARGUMENT"}),
                    },
                }
            ],
        },
        {"role": "tool", "name": "business", "tool_call_id": "work", "content": tool_body},
    ]
    expected = "\n".join(comp._render_message_for_summary(message) for message in history)
    await comp.summarize_conversation_segment(history)
    assert len(requests) > 2
    first_pass = []
    for messages in requests:
        assert comp._input_tokens(messages) <= 1800
        payload = messages[1]["content"].removeprefix("Historical transcript (data):\n")
        if "one contiguous fragment" in messages[0]["content"]:
            first_pass.append(payload)
    assert "".join(first_pass) == expected
    assert all(
        marker in "".join(first_pass)
        for marker in (
            "FINAL_USER_CORRECTION",
            "EXACT_LATE_ARGUMENT",
            "FINAL_RESULT_FACT",
            "call business",
        )
    )


def test_unicode_token_count_and_lossless_segmentation():
    # Construct multilingual runtime data while keeping repository sources English.
    text = (chr(0x4E00) + " cafe\u0301 \U0001f680 <|endoftext|> ") * 300
    pieces = split_transcript(text, 60)
    assert "".join(pieces) == text
    assert all(count_tokens(piece) <= 60 for piece in pieces)
    comp = compressor()
    messages = [{"role": "user", "content": text}]
    assert comp.calculate_history_tokens(messages) == count_tokens(json.dumps(messages, ensure_ascii=False))
    assert comp.calculate_history_tokens(messages) < count_tokens(json.dumps(messages))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        response("<summary>cut off", "length"),
        response(""),
        response("   "),
        response("<analysis>Only analysis</analysis>"),
        response("<summary>missing end"),
        response("<summary></summary>"),
        response("<summary>A<summary>B</summary></summary>"),
        SimpleNamespace(choices=[]),
        response(None),
    ],
)
async def test_invalid_summary_keeps_original_history_and_tools(reply):
    create = AsyncMock(return_value=reply)
    comp = compressor(create)
    callback = Mock()
    comp.compaction_callbacks.append(callback)
    history = large_history()
    saved = copy.deepcopy(history)
    assert await comp.compress_history(history) is history
    assert history == saved
    callback.assert_not_called()
    count = create.await_count
    assert await comp.compress_history(history) is history
    assert create.await_count == count


@pytest.mark.asyncio
async def test_valid_summary_discards_analysis_and_preserves_latest_user_turn():
    comp = HistoryCompressor(
        max_history_tokens=10,
        min_messages_to_keep=1,
        async_client=SimpleNamespace(),
    )
    create = AsyncMock(
        return_value=response("<analysis>Private scratchpad</analysis><summary>Verified facts.</summary>")
    )
    comp._llm_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    history = large_history() + [{"role": "user", "content": "Now continue with the chosen plan."}]
    final = await comp.compress_history(history)
    assert final is not history and "Private scratchpad" not in json.dumps(final)
    assert "Verified facts." in final[0]["content"]
    assert final[-1] == history[-1]
    assert comp.compression_count == 1 and comp.tokens_saved > 0


@pytest.mark.asyncio
async def test_request_failure_and_nonshrinking_summary_are_not_committed():
    history = large_history()
    for create in (
        AsyncMock(side_effect=RuntimeError("offline")),
        AsyncMock(return_value=response("<summary>" + history[0]["content"] * 2 + "</summary>")),
    ):
        comp = compressor(create)
        assert await comp.compress_history(history) is history
        assert comp.compression_count == 0 and comp.tokens_saved == 0


@pytest.mark.asyncio
async def test_all_segments_share_one_deadline_and_no_retry_loop():
    attempts = 0

    async def create(**kwargs):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.03)
        return response()

    comp = compressor(create, timeout_seconds=0.05, max_input_tokens=1500)
    history = large_history()
    assert await comp.compress_history(history) is history
    assert attempts >= 2
    count = attempts
    assert await comp.compress_history(history) is history
    assert attempts == count


@pytest.mark.asyncio
async def test_external_cancellation_is_propagated():
    started = asyncio.Event()

    async def create(**kwargs):
        started.set()
        await asyncio.Event().wait()

    comp = compressor(create)
    task = asyncio.create_task(comp.compress_history(large_history()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not comp._disabled_for_run


def tool_group():
    return [
        {"role": "user", "content": "two tasks"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": name, "function": {"name": "business", "arguments": "{}"}} for name in ("a", "b")
            ],
        },
        {"role": "tool", "tool_call_id": "b", "content": "result b"},
        {"role": "tool", "tool_call_id": "a", "content": "result a"},
    ]


def test_hot_tail_keeps_parallel_pairs_and_respects_token_budget():
    comp = HistoryCompressor(max_history_tokens=10, min_messages_to_keep=1, async_client=SimpleNamespace())
    history = tool_group()
    older, tail = comp.split_hot_tail(history)
    assert older == history[:1] and tail == history[1:]
    comp.max_tail_tokens = 2
    older, tail = comp.split_hot_tail(history)
    assert older == history and tail == []
    incomplete = history[:2]
    assert comp.split_hot_tail(incomplete) == (incomplete, [])


def test_light_mode_never_discards_tool_facts():
    text = "early " + "long evidence " * 2000 + "LATE_FACT"
    pretty = json.dumps({"text": text, "numbers": list(range(1000))}, indent=2)
    comp = compressor(compression_level="light")
    history = [{"role": "tool", "content": text}, {"role": "tool", "content": pretty}]
    compacted = comp.compress_tool_results(history)
    assert compacted[0]["content"] == text
    assert json.loads(compacted[1]["content"]) == json.loads(pretty)
    precise = (
        '{ "decimal": 0.12345678901234567890123456789, "text": "  exact spaces  ", "padding": "'
        + "x" * 6000
        + '" }'
    )
    compact = comp.compress_tool_results([{"role": "tool", "content": precise}])[0]["content"]
    assert "0.12345678901234567890123456789" in compact and "  exact spaces  " in compact


def test_recovery_index_is_bounded_typed_and_survives_repeated_compaction():
    messages = [{"role": "user", "content": "[User-uploaded images]\nhttps://example.com/input.png"}]
    messages += [
        {
            "role": "tool",
            "name": "business",
            "content": json.dumps(
                {
                    "file_url": f"https://example.com/report-{i}.pdf?signature=exact",
                    "source": {"file_url": "https://example.com/untrusted-nested"},
                }
            ),
        }
        for i in range(60)
    ]
    index, refs = render_index(messages, [], [], 1200)
    assert len(index) <= 1200
    assert "report-59.pdf?signature=exact" in index and "input.png" in index
    assert "untrusted-nested" not in index
    assert all(ref["url"] in index for ref in refs)
    assert [ref for ref in refs if ref["kind"] == "upload"]
    summary = {"role": "user", "content": "<summary>Facts.</summary>\n" + index, "context_assets": refs}
    new = {"role": "tool", "name": "business", "content": '{"file_url":"https://example.com/new.pdf"}'}
    second, selected = render_index([summary, new], [], [], 1200)
    assert "new.pdf" in second and "report-59.pdf?signature=exact" in second
    assert collect_assets([summary]) == refs
    assert len(selected) <= 100


@pytest.mark.asyncio
async def test_unrecoverable_reference_budget_keeps_original_without_calling_llm():
    create = AsyncMock(return_value=response())
    comp = compressor(create, asset_index_chars=100)
    history = large_history() + [
        {"role": "tool", "content": json.dumps({"file_url": "https://example.com/" + "x" * 500})}
    ]
    assert await comp.compress_history(history) is history
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_summary_is_transcribed_as_history_without_nested_asset_indexes():
    comp = compressor()
    old = {"role": "user", "content": "<summary>Old state.</summary>\n" + ASSET_MARK + "\nold-index-text"}
    transcript = comp._render_message_for_summary(old)
    assert transcript.startswith("history_summary:")
    assert "Old state." in transcript and "old-index-text" not in transcript


@pytest.mark.asyncio
async def test_summary_merge_has_a_bounded_number_of_rounds():
    comp = compressor(max_input_tokens=1200)

    async def echo(transcript, **kwargs):
        return transcript

    comp._summarize_transcript = echo
    with pytest.raises(CompressionUnavailable):
        await comp.summarize_conversation_segment(large_history())
