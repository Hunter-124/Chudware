"""Jurisdiction adoption metadata for building-code compliance.

Bundles edition-adoption facts (IBC/ASCE 7/NDS edition numbers + residential code) for
US states.  **No code text is stored here** — only which edition a jurisdiction has
adopted, sourced from publicly available state legislative records.

The lookup is deliberately simple: state-code → dict of edition strings.  Downstream
callers (``code_check``, ``agentic_rag``) use the edition labels to form accurate
citation references without quoting copyrighted standard text.

Usage::

    from services.structural.jurisdiction_meta import get_jurisdiction, list_jurisdictions

    meta = get_jurisdiction("CA")
    # {"ibc_edition": "2022", "asce7_edition": "22", "nds_edition": "2018",
    #  "residential_code": "CRC-2022", "notes": "..."}

    meta = get_jurisdiction("ZZ")  # unknown → raises KeyError
"""
from __future__ import annotations

import json
import os
from typing import Optional

# --------------------------------------------------------------------------- data path

_DATA_FILE = os.path.join(
    os.path.dirname(__file__),  # services/structural/
    "data",
    "jurisdiction_meta.json",
)

_CACHE: Optional[dict] = None


def _load() -> dict:
    """Load and cache the jurisdiction metadata JSON (lazy, once)."""
    global _CACHE
    if _CACHE is None:
        with open(_DATA_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # Strip the comment key and any private keys starting with '_'
        _CACHE = {k: v for k, v in raw.items() if not k.startswith("_")}
    return _CACHE


# --------------------------------------------------------------------------- public API


def get_jurisdiction(state_code: str) -> dict:
    """Return adoption metadata for a US state code (e.g. ``"CA"``, ``"TX"``).

    Parameters
    ----------
    state_code:
        Two-letter uppercase US state abbreviation.  Case is normalised automatically.

    Returns
    -------
    ``dict`` with keys ``ibc_edition``, ``asce7_edition``, ``nds_edition``,
    ``residential_code``, ``notes``.

    Raises
    ------
    KeyError
        If ``state_code`` is not in the bundled metadata.  Callers should catch this and
        degrade gracefully (e.g. omit jurisdiction-specific citations).
    """
    key = (state_code or "").strip().upper()
    data = _load()
    if key not in data:
        raise KeyError(
            f"Jurisdiction {state_code!r} not in bundled metadata. "
            "Known states: " + ", ".join(sorted(data))
        )
    return dict(data[key])  # defensive copy


def list_jurisdictions() -> list[str]:
    """Return sorted list of known state codes."""
    return sorted(_load().keys())


def jurisdiction_label(state_code: str) -> str:
    """Human-readable edition label for citation use, e.g. ``"IBC-2022/ASCE7-22"``."""
    try:
        meta = get_jurisdiction(state_code)
        return f"IBC-{meta['ibc_edition']}/ASCE7-{meta['asce7_edition']}"
    except KeyError:
        return "IBC (edition unknown)"
