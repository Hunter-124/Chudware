"""Tests for services/parts/model_matcher.py — deterministic product→model matcher.

Offline: no Playwright, no network. We monkeypatch the registered CAD-model
adapters' ``search`` so the fan-out sees a controlled candidate set. Style mirrors
services/browser/adapters/test_new_adapters.py (patch the seam, assert the shape).

Coverage of contracts/sourcing.md §2a:
  * QUALITY: an exact-MPN hit AND a title-only near-miss in the same candidate set
    → the exact match ranks first with attach:true; the near-miss has attach:false.
  * a fuzzy-only product → best is a non-exact candidate (attach:false) + a
    reverse_engineer fallback.
  * a standard-MPN (bearing "6205") resolves via spec_sheet_matcher — no repo search.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.parts import model_matcher as mm  # noqa: E402


# ---------------------------------------------------------------------------
# Fake adapter fan-out
# ---------------------------------------------------------------------------

class _FakeAdapter:
    def __init__(self, key: str, hits: list[dict]):
        self.key = key
        self._hits = hits

    def search(self, query: str) -> list[dict]:  # noqa: ARG002
        return list(self._hits)


@pytest.fixture()
def patch_adapters(monkeypatch):
    """Install a controlled set of CAD-model adapters for the fan-out."""
    def _install(mapping: dict[str, list[dict]]):
        adapters = [_FakeAdapter(k, v) for k, v in mapping.items()]
        monkeypatch.setattr(mm, "_cad_adapters", lambda: adapters)
        return adapters
    return _install


# ---------------------------------------------------------------------------
# STEP 1 — standard-hardware short-circuit
# ---------------------------------------------------------------------------

def test_standard_mpn_resolves_without_repo_search(monkeypatch):
    # If the fan-out were reached, this adapter would blow up — proving STEP 1
    # short-circuits before any repo search.
    class _Boom:
        key = "grabcad"

        def search(self, query):  # noqa: ARG002
            raise AssertionError("repo search must not run for a standard MPN")

    monkeypatch.setattr(mm, "_cad_adapters", lambda: [_Boom()])
    r = mm.match_model({"title": "Ball bearing", "mpn": "6205"})
    assert r["ok"] is True
    assert r["fallback"] is None
    assert r["best"] is not None
    assert r["best"]["confidence"] == "exact"
    assert r["best"]["attach"] is True
    assert r["coverage"]["sites_searched"] == 0
    assert r["best"]["artifact_plan"]["how"] in ("make_standard_part", "part_place")


# ---------------------------------------------------------------------------
# QUALITY — exact MPN beats a title-only near-miss
# ---------------------------------------------------------------------------

def test_exact_mpn_ranks_first_and_only_it_attaches(patch_adapters):
    exact_hit = {"title": "Linkage Bracket 1LNK-4B", "url": "https://grabcad.com/library/exact",
                 "site": "grabcad", "mpn": "1LNK-4B"}
    near_miss = {"title": "Generic Linkage Bracket", "url": "https://grabcad.com/library/near",
                 "site": "grabcad"}
    patch_adapters({"grabcad": [near_miss, exact_hit]})

    r = mm.match_model({"title": "Linkage Bracket", "mpn": "1LNK-4B", "brand": "AcmeCorp"})

    assert r["ok"] is True
    assert r["matches"], "expected at least the exact + near-miss"
    top = r["matches"][0]
    assert top["confidence"] == "exact"
    assert top["attach"] is True
    assert top["part_id"].endswith("/exact")
    # the near-miss is present, ranked below, and never auto-attaches
    near = next(m for m in r["matches"] if m["part_id"].endswith("/near"))
    assert near["confidence"] == "weak"
    assert near["attach"] is False
    assert top["score"] > near["score"]
    # an exact match exists → no reverse_engineer fallback
    assert r["fallback"] is None
    assert r["best"]["attach"] is True


def test_normalized_mpn_equality_is_exact(patch_adapters):
    """Vendor packaging cruft on the hit's PN still resolves to an exact match."""
    hit = {"title": "Cap STM32", "url": "https://grabcad.com/library/x",
           "site": "grabcad", "mpn": "608-2RS-CT-ND"}
    patch_adapters({"grabcad": [hit]})
    r = mm.match_model({"title": "bearing", "mpn": "608-2RS"})
    assert r["matches"][0]["confidence"] == "exact"
    assert r["matches"][0]["attach"] is True


# ---------------------------------------------------------------------------
# Fuzzy-only — best non-exact + reverse_engineer fallback
# ---------------------------------------------------------------------------

def test_fuzzy_only_offers_reverse_engineer_fallback(patch_adapters):
    weak = {"title": "Widget Housing Assembly", "url": "https://grabcad.com/library/w",
            "site": "grabcad"}
    patch_adapters({"grabcad": [weak]})

    r = mm.match_model({"title": "Widget Housing", "mpn": "NOPE-999",
                        "brand": "Acme", "source_url": "https://shop.example/widget"})

    assert r["ok"] is True
    assert r["best"] is not None
    assert r["best"]["confidence"] != "exact"
    assert r["best"]["attach"] is False
    fb = r["fallback"]
    assert fb is not None
    assert fb["how"] == "reverse_engineer"
    seed = fb["seed"]
    assert seed["title"] == "Widget Housing"
    assert seed["mpn"] == "NOPE-999"
    assert seed["brand"] == "Acme"
    assert seed["source_url"] == "https://shop.example/widget"
    assert seed["images"] == [] and seed["documents"] == []


def test_strong_tier_brand_plus_substring(patch_adapters):
    hit = {"title": "Motor EC 45", "url": "https://maxon.example/x", "site": "maxon",
           "brand": "maxon", "mpn": "EC45-100-FLAT"}
    patch_adapters({"maxon": [hit]})
    r = mm.match_model({"title": "motor", "mpn": "EC45-100", "brand": "maxon"})
    assert r["matches"][0]["confidence"] == "strong"
    assert r["matches"][0]["attach"] is False
    assert r["fallback"] is not None  # no exact → RE offered


# ---------------------------------------------------------------------------
# Honesty + robustness
# ---------------------------------------------------------------------------

def test_login_required_contributes_zero_but_counts_coverage(patch_adapters):
    login = {"ok": False, "reason": "login_required", "hint": "sign in"}
    good = {"title": "Bracket X1", "url": "https://grabcad.com/library/x",
            "site": "grabcad", "mpn": "X1"}
    patch_adapters({"traceparts": [login], "grabcad": [good]})

    r = mm.match_model({"title": "Bracket", "mpn": "X1"})
    assert r["coverage"]["sites_searched"] == 2
    assert r["coverage"]["raw_hits"] == 1  # login response is not a hit
    assert [m["part_id"] for m in r["matches"]] == ["catalog:grabcad::https://grabcad.com/library/x"]


def test_bad_adapter_does_not_sink_fanout(monkeypatch):
    class _Boom:
        key = "grabcad"

        def search(self, query):  # noqa: ARG002
            raise RuntimeError("boom")

    class _Ok:
        key = "traceparts"

        def search(self, query):  # noqa: ARG002
            return [{"title": "T1", "url": "https://traceparts.example/t", "site": "traceparts",
                     "mpn": "T1"}]

    monkeypatch.setattr(mm, "_cad_adapters", lambda: [_Boom(), _Ok()])
    r = mm.match_model({"title": "thing", "mpn": "T1"})
    assert r["ok"] is True
    assert r["matches"][0]["confidence"] == "exact"


def test_deterministic(patch_adapters):
    hits = [{"title": "A", "url": "https://grabcad.com/a", "site": "grabcad", "mpn": "Z"},
            {"title": "B", "url": "https://grabcad.com/b", "site": "grabcad", "mpn": "Z"}]
    patch_adapters({"grabcad": list(hits)})
    a = mm.match_model({"title": "thing", "mpn": "Z"})
    patch_adapters({"grabcad": list(hits)})
    b = mm.match_model({"title": "thing", "mpn": "Z"})
    assert a == b


def test_bad_input_is_contract_shaped():
    assert mm.match_model("not a dict")["ok"] is False  # type: ignore[arg-type]
    r = mm.match_model({})
    assert r["ok"] is False and r["reason"] and r["hint"]


def test_partrecord_extraction_from_params(patch_adapters):
    """A PartRecord carries mpn/brand in params — match_model must find them."""
    hit = {"title": "Prod 42", "url": "https://grabcad.com/p", "site": "grabcad", "mpn": "PN42"}
    patch_adapters({"grabcad": [hit]})
    rec = {"part_id": "catalog:amazon::https://amazon.example/p",
           "title": "Some Product", "params": {"mpn": "PN42", "brand": "Acme"}}
    r = mm.match_model(rec)
    assert r["product"]["mpn"] == "PN42"
    assert r["product"]["brand"] == "Acme"
    assert r["matches"][0]["confidence"] == "exact"
