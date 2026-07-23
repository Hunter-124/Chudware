"""Canonical document JSON serialization policy (T-H2).

Policy (mirrors the C++ implementation in core/doc/stepplus.cpp):
  - All JSON floats are rounded to 6 decimal places (µm resolution for mm lengths;
    also covers 1e-3 deg resolution for angles since 1e-6 deg ≫ needed precision).
    Applied at SERIALIZE time only — in-memory doubles are never touched.
  - Keys are sorted (sort_keys=True) so object-key order is deterministic.
  - indent=2 for human-readable output matching the existing format.

Usage::

    from services.plm.canonical import canonical_dump, round_floats_doc

    # Serialize a document dict to a canonical JSON string:
    s = canonical_dump(doc_dict)

    # Round floats in a document without serializing (e.g. for diffing):
    rounded = round_floats_doc(doc_dict)
"""
from __future__ import annotations

import json
import math
from typing import Any

# Resolution: 6 dp = 1 µm for mm-unit lengths; 1e-3 dp for degrees.
# Both are stored as the same float type, so a single pass at 6 dp is correct.
_DECIMALS = 6
_FACTOR = 10 ** _DECIMALS


def round_floats_doc(obj: Any) -> Any:
    """Recursively round every float in *obj* to ``_DECIMALS`` decimal places.

    Non-float values (bool, int, str, None) pass through unchanged.  The
    returned value is a new object; the input is not mutated.
    """
    if isinstance(obj, float):
        return round(obj, _DECIMALS)
    if isinstance(obj, list):
        return [round_floats_doc(v) for v in obj]
    if isinstance(obj, dict):
        return {k: round_floats_doc(v) for k, v in obj.items()}
    return obj


def canonical_dump(doc: dict | Any) -> str:
    """Return the canonical JSON serialization of *doc*.

    Floats are rounded (µm grade), keys sorted, indent=2.  The result is
    byte-stable: calling ``canonical_dump(canonical_dump_parsed(s)) == s``
    for any already-canonical string *s*.
    """
    return json.dumps(round_floats_doc(doc), indent=2, sort_keys=True)
