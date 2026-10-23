"""services/research — research and engineering-knowledge tools.

Contract: ``contracts/research_tools.md``. ``web_search`` and ``fetch_page`` use a
provider interface and per-user local knowledge cache, with offline/ITAR degradation.
Material, thread, section and datasheet helpers share the §1 answer envelope.
Engineering calculators, including ``feeds_speeds`` and ``tap_drill``, use
``aicad.calc/1`` (``contracts/advisor.md`` §7).
"""
from __future__ import annotations

from . import common, providers
from .cache import Cache, knowledge_root
from .datasheet_fetch import datasheet_fetch
from .eng_calc import eng_calc
from .fetch_page import fetch_page
from .fits import fit_calc, recommend_fit, tolerance_band
from .machining import check_envelope, feeds_speeds, tap_drill
from .material_lookup import material_lookup
from .standard_section import standard_section
from .thread_spec import thread_spec
from .unit_convert import unit_convert
from .web_search import web_search

__all__ = [
    "web_search",
    "fetch_page",
    # Source helpers (same §1 answer envelope)
    "material_lookup",
    "thread_spec",
    "standard_section",
    "datasheet_fetch",
    "unit_convert",
    "eng_calc",
    # ISO 286 fit calculator (aicad.calc/1 envelope)
    "fit_calc",
    "recommend_fit",
    "tolerance_band",
    "Cache",
    "knowledge_root",
    "common",
    "providers",
    # feeds/speeds + tap drill (aicad.calc/1 + aicad.advisory/1)
    "feeds_speeds",
    "tap_drill",
    "check_envelope",
]
