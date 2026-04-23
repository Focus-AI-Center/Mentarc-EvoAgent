import tiktoken

_encoding = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens in a text string."""
    return len(_encoding.encode(text))


def count_messages_tokens(messages: list[dict]) -> int:
    """Count total tokens in a list of OpenAI-format messages."""
    total = 0
    for msg in messages:
        total += 4  # message overhead
        for value in msg.values():
            if isinstance(value, str):
                total += count_tokens(value)
    total += 2  # reply priming
    return total
