"""Local BM25F ranking over tool names, descriptions and parameter schemas."""

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

FIELD_WEIGHTS = (4.0, 2.0, 1.0)
FIELD_B = (0.0, 0.75, 0.75)
K1 = 1.2
STOP_WORDS = frozenset({"the", "a", "an", "to", "for", "please", "tool", "tools", "use", "call", "need"})


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def terms(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    result = []
    for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        if "\u3400" <= token[0] <= "\u9fff" and len(token) > 1:
            result.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            result.append(token)
    return result


@dataclass(frozen=True)
class FieldIndex:
    counts: tuple[Counter, ...]
    lengths: tuple[int, ...]

    @classmethod
    def build(cls, name: str, description: str, parameters: str):
        counts = tuple(Counter(terms(text)) for text in (name, description, parameters))
        return cls(counts, tuple(sum(field.values()) for field in counts))


class BM25Index:
    def __init__(self, documents: list[FieldIndex]):
        self.documents = documents
        count = len(documents) or 1
        self.averages = tuple(
            sum(document.lengths[index] for document in documents) / count or 1.0
            for index in range(len(FIELD_WEIGHTS))
        )

    def scores(self, query: str) -> list[float]:
        query_terms = set(terms(query)) - STOP_WORDS
        count = len(self.documents)
        frequencies = {
            term: sum(any(term in field for field in document.counts) for document in self.documents)
            for term in query_terms
        }
        scores = []
        for document in self.documents:
            score = 0.0
            for term in sorted(query_terms):
                frequency = frequencies[term]
                if not frequency:
                    continue
                weighted = 0.0
                for index, (weight, adjustment) in enumerate(zip(FIELD_WEIGHTS, FIELD_B, strict=True)):
                    occurrences = document.counts[index].get(term, 0)
                    if occurrences:
                        length = 1 - adjustment + adjustment * document.lengths[index] / self.averages[index]
                        weighted += weight * occurrences / length
                if weighted:
                    inverse_frequency = math.log1p((count - frequency + 0.5) / (frequency + 0.5))
                    score += inverse_frequency * weighted * (K1 + 1) / (K1 + weighted)
            scores.append(score)
        return scores
