"""engine/research — Pillar 8 research & knowledge tools.

Contract: ``contracts/research_tools.md``. v1 ships ``web_search`` + ``fetch_page`` over a
provider interface and a per-user local knowledge cache, with offline/ITAR degradation.
Later tasks (T-F2+) add ``extract_structured`` / ``material_lookup`` / ``thread_spec`` /
… reusing the same §1 answer envelope.
T-W6 adds ``feeds_speeds`` + ``tap_drill`` (aicad.calc/1, contracts/advisor.md §7).
"""
from __future__ import annotations

from . import common, providers
from .cache import Cache, knowledge_root
from .datasheet_fetch import datasheet_fetch
from .eng_calc import eng_calc
from .fetch_page import fetch_page
from .machining import check_envelope, feeds_speeds, tap_drill
from .material_lookup import material_lookup
from .standard_section import standard_section
from .thread_spec import thread_spec
from .unit_convert import unit_convert
from .web_search import web_search

__all__ = [
    "web_search",
    "fetch_page",
    # T-F2 source helpers (same §1 answer envelope)
    "material_lookup",
    "thread_spec",
    "standard_section",
    "datasheet_fetch",
    "unit_convert",
    "eng_calc",
    "Cache",
    "knowledge_root",
    "common",
    "providers",
    # T-W6: feeds/speeds + tap drill (aicad.calc/1 + aicad.advisory/1)
    "feeds_speeds",
    "tap_drill",
    "check_envelope",
]
