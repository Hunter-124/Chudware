"""Manufacturer part-number (MPN) normalization — the one Python seam onto the C++
sourcing core's ``normalize_mpn`` RPC (core/sourcing/sourcing.cpp).

The canonical normalizer lives only in the core; this module adds no rules of its own.
Replies and core failures (``core_bridge.CoreError``, library load errors) pass
through unchanged; callers retain their own result and degraded contracts.
"""
from __future__ import annotations

from collections.abc import Callable


def core_normalize_mpn(mpn: str, call: Callable[..., object]) -> object:
    """Issue the normalization request without interpreting its reply.

    The caller supplies its bridge binding to preserve eager/lazy import behavior.
    """
    return call("normalize_mpn", {"mpn": mpn})
