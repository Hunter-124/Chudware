"""Lossless canonical document JSON serialization.

Mirrors core/doc/stepplus.cpp: preserve numeric values, sort object keys, and
indent by two spaces. Rounding axes or transforms changes saved constraints.
"""
from __future__ import annotations

import json
from typing import Any


def canonical_dump(doc: dict | Any) -> str:
    """Serialize a document deterministically without quantizing its numbers."""
    return json.dumps(doc, indent=2, sort_keys=True)
