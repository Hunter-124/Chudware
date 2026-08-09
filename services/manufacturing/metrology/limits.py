"""Shared resource ceilings for hostile metrology collections."""
from __future__ import annotations

from itertools import islice
from typing import Iterable, TypeVar


MAX_METROLOGY_ITEMS = 100_000
_T = TypeVar("_T")


class MetrologyResourceRefusal(ValueError):
    """Named refusal raised before an input collection can allocate without bound."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def bounded_tuple(
    values: Iterable[_T],
    *,
    label: str,
    limit: int = MAX_METROLOGY_ITEMS,
) -> tuple[_T, ...]:
    result = tuple(islice(values, limit + 1))
    if len(result) > limit:
        raise MetrologyResourceRefusal(
            f"{label}_limit",
            f"{label} exceeds the {limit} item limit",
        )
    return result
