"""Bounded session persistence on stream cancellation or explicit closure."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from functools import wraps

import anyio

from configs.settings import SESSION_SAVE_TIMEOUT_SECONDS
from core.runtime.history import interrupted_output
from utils.logger import logger
from utils.ws_call import ws_call

INTERRUPTION_NOTE = (
    "[Execution interrupted] This turn stopped before completion was confirmed. "
    "Completed tool results are retained. Interrupted calls mean local waiting stopped, "
    "not that the remote operation did not happen. Verify outcomes before retrying; "
    "continue only according to the user's next instruction."
)


async def _protected(operation):
    """Shield AnyIO cancellation scopes and repeated direct asyncio cancellation."""
    task = asyncio.create_task(operation)
    cancelled, error = False, None
    with anyio.CancelScope(shield=True):
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
            except Exception as exc:
                error = exc
                break
    if cancelled:
        raise asyncio.CancelledError
    if error:
        raise error


def _complete_interrupted_pairs(history):
    """Keep SDK input structurally valid without inventing successful results."""
    outputs = {message.get("tool_call_id") for message in history if message.get("role") == "tool"}
    for message in list(history):
        for call in message.get("tool_calls", []):
            if call.get("id") and call["id"] not in outputs:
                name = call.get("function", {}).get("name", "")
                history.append(
                    {
                        "role": "tool",
                        "name": name,
                        "tool_call_id": call["id"],
                        "content": interrupted_output(name),
                        "execution_status": "interrupted",
                    }
                )
                outputs.add(call["id"])
    return history


class TurnPersistence:
    """Storage is injected; this component has no SDK loop or Redis dependency."""

    def __init__(self, workspace, user_id, session_id, *, metadata=None, timeout=None, writer=None):
        self.workspace, self.user_id, self.session_id = workspace, user_id, session_id
        self.metadata = dict(metadata or {})
        self.timeout = SESSION_SAVE_TIMEOUT_SECONDS if timeout is None else timeout
        if self.timeout <= 0:
            raise ValueError("session save timeout must be positive")
        self.writer = writer
        self.start("")

    def start(self, user_input):
        self.user_input = user_input
        self.history_loaded = False
        self.user_added = False
        self.saved = False
        self.write_attempted = False
        self.snapshot = lambda: []

    def loaded(self, snapshot):
        self.history_loaded = True
        self.snapshot = snapshot

    def user_message_added(self):
        self.user_added = True

    async def save(self, history, *, interrupted=False, reason=""):
        conversation = deepcopy(history)
        record = {
            **self.metadata,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "conversation": conversation,
            "message_count": len(conversation),
        }
        if interrupted:
            record.update(interrupted=True, interruption_reason=reason)

        async def write():
            self.write_attempted = True
            async with asyncio.timeout(self.timeout):
                if self.writer:
                    await self.writer(record)
                else:
                    await ws_call(self.workspace, "history_save", self.session_id, record)
                self.saved = True

        await _protected(write())

    async def save_interrupted(self, reason):
        if not self.history_loaded or self.saved or self.write_attempted:
            return
        history = deepcopy(self.snapshot())
        if not self.user_added:
            history.append({"role": "user", "content": self.user_input})
        history = _complete_interrupted_pairs(history)
        history.append({"role": "assistant", "content": INTERRUPTION_NOTE, "execution_status": "interrupted"})
        await self.save(history, interrupted=True, reason=reason)


def persist_on_interrupt(run):
    """Small adapter: the owner only supplies a TurnPersistence instance."""

    @wraps(run)
    async def wrapped(owner, user_input, *args, **kwargs):
        persistence = getattr(owner, "_persistence", None)
        if persistence is None:
            config = getattr(owner, "config", None)
            persistence = TurnPersistence(
                owner.workspace,
                owner.user_id,
                owner.session_id,
                metadata={"agent_type": config.name} if config else None,
            )
            owner._persistence = persistence
        persistence.start(user_input)
        source = run(owner, user_input, *args, **kwargs)
        reason = ""
        try:
            async for event in source:
                yield event
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except GeneratorExit:
            reason = "closed"
            raise
        finally:
            if reason:
                # Close in the original task/context, then shield the independent
                # storage write. Async context managers may own context-local tokens.
                with anyio.CancelScope(shield=True):
                    try:
                        await source.aclose()
                    finally:
                        try:
                            await persistence.save_interrupted(reason)
                        except Exception:
                            logger.exception(
                                "Interrupted session save failed user={} session={}",
                                owner.user_id,
                                owner.session_id,
                            )
            else:
                await source.aclose()

    return wrapped
