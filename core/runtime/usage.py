"""Bind one usage collector to a turn and account for individual SDK responses."""

from contextlib import aclosing
from functools import wraps

from utils.usage import usage_scope


def capture_usage(run):
    @wraps(run)
    async def wrapped(owner, *args, **kwargs):
        with usage_scope(getattr(owner, "inherited_usage", None)) as collector:
            owner.usage_collector = collector
            async with aclosing(run(owner, *args, **kwargs)) as source:
                async for event in source:
                    yield event

    return wrapped


def record_sdk_usage(collector, result, *, run_id, model, category, response_models=None):
    if collector is None or result is None:
        return
    for index, response in enumerate(getattr(result, "raw_responses", [])):
        actual_model = (response_models or {}).get(index, model)
        collector.add(
            actual_model,
            getattr(response, "usage", None),
            category=category,
            key=(run_id, index),
        )
