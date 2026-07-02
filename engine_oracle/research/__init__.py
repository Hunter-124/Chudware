"""engine/research — Pillar 8 research & knowledge tools.

Contract: ``contracts/research_tools.md``. v1 ships ``web_search`` + ``fetch_page`` over a
provider interface and a per-user local knowledge cache, with offline/ITAR degradation.
Later tasks (T-F2+) add ``extract_structured`` / ``material_lookup`` / ``thread_spec`` /
… reusing the same §1 answer envelope.
"""
from __future__ import annotations

from . import common, providers
from .cache import Cache, knowledge_root
from .fetch_page import fetch_page
from .web_search import web_search

__all__ = [
    "web_search",
    "fetch_page",
    "Cache",
    "knowledge_root",
    "common",
    "providers",
]
