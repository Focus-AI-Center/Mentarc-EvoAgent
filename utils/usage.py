"""Request-scoped measured provider usage; context estimates are kept separate."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_current = ContextVar("request_usage", default=None)
_FIELDS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
)


def _value(value, name, default=0):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _integer(value, default=0):
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class UsageCollector:
    def __init__(self):
        self.by_model, self.by_category, self.seen = {}, {}, set()

    def add(self, model, usage, *, category, key=None):
        if usage is None or (key is not None and key in self.seen):
            return
        if key is not None:
            self.seen.add(key)
        input_details = _value(usage, "input_tokens_details", None) or _value(
            usage, "prompt_tokens_details", None
        )
        output_details = _value(usage, "output_tokens_details", None) or _value(
            usage, "completion_tokens_details", None
        )
        inputs = _integer(_value(usage, "input_tokens", _value(usage, "prompt_tokens")))
        outputs = _integer(_value(usage, "output_tokens", _value(usage, "completion_tokens")))
        row = {
            "requests": _integer(_value(usage, "requests", 1)) or 1,
            "input_tokens": inputs,
            "output_tokens": outputs,
            "total_tokens": _integer(_value(usage, "total_tokens")) or inputs + outputs,
            "cached_input_tokens": _integer(_value(input_details, "cached_tokens")),
            "reasoning_tokens": _integer(_value(output_details, "reasoning_tokens")),
        }
        for group, name in ((self.by_model, model or "unknown"), (self.by_category, category)):
            target = group.setdefault(str(name), dict.fromkeys(_FIELDS, 0))
            for field in _FIELDS:
                target[field] += row[field]

    def snapshot(self):
        total = {field: sum(row[field] for row in self.by_model.values()) for field in _FIELDS}
        return {
            "usage": total,
            "usage_by_model": deepcopy(self.by_model),
            "usage_by_category": deepcopy(self.by_category),
        }


def current_usage():
    return _current.get()


@contextmanager
def usage_scope(collector=None):
    collector = collector or UsageCollector()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def usage_payload():
    collector = current_usage()
    return collector.snapshot() if collector else UsageCollector().snapshot()


def record_provider_usage(response, *, category, model=None):
    collector = current_usage()
    if collector:
        collector.add(
            _value(response, "model", None) or model or "unknown",
            _value(response, "usage", None),
            category=category,
        )
