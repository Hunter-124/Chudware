"""Offline, key-free tests for the deterministic spec-sheet matcher (T-B4).

No network, no API keys, no native core required: geometry generation degrades to
``model_path: None`` when ``libaicad`` is absent (as in CI), and every matcher path is pure
Python over the bundled clean-room ISO tables.
"""
from __future__ import annotations

import json
import os

import pytest

from engine import bolts_loader
from engine import spec_sheet_matcher as ssm


# --------------------------------------------------------------------------------------
# normalize_mpn — vendor/packaging suffix stripping
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("M3x8-ND", "M3x8"),            # DigiKey distributor
    ("M3x8-CT", "M3x8"),            # cut tape
    ("M3x8-TR", "M3x8"),            # tape & reel
    ("M3x8-TB", "M3x8"),            # tube
    ("M3x8-DKR", "M3x8"),           # DigiReel
    ("M3x8-BULK", "M3x8"),          # bulk
    ("M3x8-REEL", "M3x8"),          # full reel
    ("M3x8-TAPE", "M3x8"),          # tape pack
    ("M3x8-PBFREE", "M3x8"),        # Pb-free marketing
    ("M3x8-EA", "M3x8"),            # each
    ("M3x8-CT-ND", "M3x8"),         # stacked: cut tape + distributor
    ("M3x8-TR-ND", "M3x8"),         # stacked: reel + distributor
    ("M3x8-1-ND", "M3x8"),          # DigiKey quantity qualifier
    ("M3x8-6-ND", "M3x8"),          # DigiKey quantity qualifier
    ("608-2RS-CT-ND", "608-2RS"),   # keep the seal code, drop packaging
    ("GRM188R71C104(CutTape)", "GRM188R71C104"),  # parenthetical packaging
])
def test_normalize_strips_vendor_suffixes(raw, expected):
    assert ssm.normalize_mpn(raw) == expected


def test_normalize_preserves_legit_numeric_suffix():
    # A bare numeric suffix (not glued to a distributor token) is a real PN part.
    assert ssm.normalize_mpn("91251A542") == "91251A542"
    assert ssm.normalize_mpn("PART-100") == "PART-100"
    assert ssm.normalize_mpn("91251A542-1-ND") == "91251A542"


def test_normalize_covers_at_least_ten_suffixes():
    # DoD: >= 10 distinct vendor suffixes recognized.
    distinct = {"-ND", "-CT", "-TR", "-TB", "-DKR", "-BULK", "-REEL", "-TAPE",
                "-PBFREE", "-EA", "-CT-ND", "-TR-ND", "-1-ND"}
    stripped = {s for s in distinct if ssm.normalize_mpn("M3x8" + s) == "M3x8"}
    assert len(stripped) >= 10


def test_normalize_idempotent():
    once = ssm.normalize_mpn("M3x8-CT-ND")
    assert ssm.normalize_mpn(once) == once


# --------------------------------------------------------------------------------------
# bolts_loader — clean-room table loading
# --------------------------------------------------------------------------------------

def test_loader_loads_bundled_tables():
    tables = bolts_loader.load_bolts_tables()
    assert "ISO 4762" in tables            # SHCS
    assert "ISO 15" in tables              # bearings
    shcs = tables["ISO 4762"]
    assert shcs.family == "shcs"
    assert shcs.needs_length is True
    assert "M3" in shcs.sizes
    assert shcs.get("m3") is not None      # format-tolerant lookup


def test_loader_missing_dir_is_empty_not_fatal(tmp_path):
    assert bolts_loader.load_bolts_tables(tmp_path / "nope") == {}


def test_designation_index_includes_aliases():
    idx = bolts_loader.designation_index()
    assert "ISO 4762" in idx
    assert "DIN 912" in idx                # alias of ISO 4762


# --------------------------------------------------------------------------------------
# match_bolts — fasteners, bearings, misses (no fabrication)
# --------------------------------------------------------------------------------------

def test_match_bolts_metric_screw_params():
    m = ssm.match_bolts("M3x8-ND")
    assert m is not None
    assert m["source"] == "bolts"
    assert m["family"] == "shcs"
    assert m["size"] == "M3"
    assert m["length_mm"] == 8.0
    assert m["params"]["d"] == 3.0
    assert m["params"]["pitch"] == 0.5
    assert m["catalog_id"] == "ISO 4762 M3x8"   # provenance


def test_match_bolts_default_family_is_screw():
    assert ssm.match_bolts("M6")["family"] == "shcs"


def test_match_bolts_family_hints():
    assert ssm.match_bolts("M6 hex nut")["family"] == "hex_nut"
    assert ssm.match_bolts("M4 flat washer")["family"] == "washer_flat"
    assert ssm.match_bolts("M5 button head")["family"] == "bhcs"


def test_match_bolts_bearing_with_seal_code():
    m = ssm.match_bolts("608-2RS-CT-ND")
    assert m is not None
    assert m["family"] == "bearing"
    assert m["size"] == "608"
    assert m["params"] == {"bore": 8.0, "outer_d": 22.0, "width": 7.0}
    assert m["standard"] == "ISO 15"


def test_match_bolts_explicit_standard():
    m = ssm.match_bolts("ISO 4762 M5x20")
    assert m is not None
    assert m["size"] == "M5"
    assert m["length_mm"] == 20.0


def test_match_bolts_miss_returns_none():
    assert ssm.match_bolts("XYZ-NONSENSE-123") is None
    assert ssm.match_bolts("") is None


# --------------------------------------------------------------------------------------
# match_kicad — user-supplied libraries only (clean-room)
# --------------------------------------------------------------------------------------

_KICAD_SYM = """(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "MyPart" (in_bom yes) (on_board yes)
    (property "Reference" "U" (id 0) (at 0 0 0))
    (property "Value" "MyPart" (id 1) (at 0 0 0))
    (property "Footprint" "Package_SO:SOIC-8" (id 2) (at 0 0 0))
    (property "MPN" "ABC-123" (id 4) (at 0 0 0))
  )
)
"""


def test_match_kicad_no_config_returns_none(monkeypatch):
    monkeypatch.delenv("AICAD_KICAD_SYM_PATH", raising=False)
    assert ssm.match_kicad("ABC-123") is None


def test_match_kicad_reads_user_library(tmp_path, monkeypatch):
    lib = tmp_path / "MyLib.kicad_sym"
    lib.write_text(_KICAD_SYM, encoding="utf-8")
    monkeypatch.setenv("AICAD_KICAD_SYM_PATH", str(tmp_path))
    m = ssm.match_kicad("ABC-123-ND")   # normalized -> ABC-123
    assert m is not None
    assert m["source"] == "kicad"
    assert m["library"] == str(lib)
    assert m["params"].get("MPN") == "ABC-123"   # from the symbol's MPN field
    assert m["catalog_id"].startswith("kicad:")


def test_match_kicad_miss_returns_none(tmp_path, monkeypatch):
    lib = tmp_path / "MyLib.kicad_sym"
    lib.write_text(_KICAD_SYM, encoding="utf-8")
    monkeypatch.setenv("AICAD_KICAD_SYM_PATH", str(tmp_path))
    assert ssm.match_kicad("SOMETHING-ELSE") is None


# --------------------------------------------------------------------------------------
# generate_model — graceful without the native core
# --------------------------------------------------------------------------------------

def _core_available() -> bool:
    try:
        import sys
        from pathlib import Path
        svc = Path(ssm.__file__).resolve().parent.parent / "services" / "mcp"
        if str(svc) not in sys.path:
            sys.path.insert(0, str(svc))
        import core_bridge as cb  # type: ignore
        cb.call("parts_catalog_list", {})
        return True
    except Exception:
        return False


def test_generate_model_kicad_match_is_none():
    # KiCad metadata matches are spec-reference only, never buildable geometry.
    assert ssm.generate_model({"source": "kicad", "family": None}) is None


def test_generate_model_degrades_or_builds():
    match = ssm.match_bolts("M3x8")
    path = ssm.generate_model(match)
    if _core_available():
        assert path and os.path.exists(path)
    else:
        assert path is None   # contract "no model" offline, never a traceback


# --------------------------------------------------------------------------------------
# match_part_number — orchestrator (contract shape)
# --------------------------------------------------------------------------------------

def test_match_part_number_hit_shape():
    r = ssm.match_part_number("M3x8-ND")
    assert r["matched"] is True
    assert r["source"] == "bolts"
    assert r["normalized"] == "M3x8"
    assert r["family"] == "shcs"
    assert r["size"] == "M3"
    assert r["params"]["d"] == 3.0
    assert "model_path" in r               # populated on a core host, None offline
    if not _core_available():
        assert r["model_path"] is None


def test_match_part_number_miss_is_contract_unknown():
    r = ssm.match_part_number("TOTALLY-UNKNOWN-PART")
    assert r["matched"] is False
    assert r["source"] is None
    assert r["params"] is None
    assert r["model_path"] is None


def test_match_part_number_no_generate_skips_model():
    r = ssm.match_part_number("M3x8", generate=False)
    assert r["matched"] is True
    assert r["model_path"] is None


# --------------------------------------------------------------------------------------
# MCP tool registration
# --------------------------------------------------------------------------------------

def test_mcp_tool_registers_and_returns_json():
    from mcp_server.spec_tools import register_spec_tools

    class _FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    m = _FakeMCP()
    register_spec_tools(m)
    assert "match_part_number" in m.tools
    out = json.loads(m.tools["match_part_number"]("M3x8-ND"))
    assert out["ok"] is True
    assert out["matched"] is True
    assert out["family"] == "shcs"
    assert "model_path" in out
