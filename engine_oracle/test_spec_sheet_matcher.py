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


# ======================================================================================
# AI path (T-B5) — datasheet -> LLM dims -> parametric template -> VendorPart
# ======================================================================================

# A pasted radial-electrolytic-capacitor datasheet (the DoD fixture).
_CAP_DATASHEET = """
Panasonic EEU-FR1V101  Aluminium Electrolytic Capacitor (Radial, can type)
Capacitance 100 uF   Rated voltage 35 V DC   Tolerance +/- 20 %
Case size D x L: 6.3 mm x 11.2 mm   Lead spacing 2.5 mm   Lead diameter 0.6 mm
"""


class _MockLLM:
    """A scripted provider (T-F9 Provider shape) returning a fixed JSON reply."""

    name = "mock-llm"
    supports_vision = False

    def __init__(self, reply: str):
        self._reply = reply
        self.calls: list[tuple[str, list]] = []

    def complete(self, system, messages, max_tokens=8000):
        self.calls.append((system, messages))
        return self._reply

    def complete_ex(self, system, messages, max_tokens=8000):
        return self.complete(system, messages, max_tokens=max_tokens), ""


_CAP_JSON = (
    '```json\n{"body_diameter_mm": 6.3, "body_length_mm": 11.2, "lead_spacing_mm": 2.5, '
    '"lead_diameter_mm": 0.6, "capacitance_uf": 100, "voltage_v": 35, "tolerance_pct": 20, '
    '"mpn": "EEU-FR1V101", "description": "100uF 35V radial electrolytic"}\n```'
)


def _isolate_registry(tmp_path, monkeypatch):
    """Point the vendor-parts cache + index at a tmp dir so register_* never touches real state."""
    from engine import vendor_parts
    monkeypatch.setattr(vendor_parts.paths, "VENDOR_PARTS", tmp_path / "vendor_parts")
    monkeypatch.setattr(vendor_parts, "INDEX", tmp_path / "vendor_parts" / "index.json")


def test_ai_extract_dims_from_capacitor_datasheet():
    prov = _MockLLM(_CAP_JSON)
    out = ssm.ai_extract_dims(_CAP_DATASHEET, "capacitor_radial", provider=prov)
    assert out["template"] == "capacitor_radial"
    assert out["dims"]["body_diameter_mm"] == 6.3
    assert out["dims"]["body_length_mm"] == 11.2
    assert out["dims"]["capacitance_uf"] == 100.0
    assert out["missing_required"] == []
    assert out["mpn"] == "EEU-FR1V101"
    # The datasheet text was actually handed to the model.
    assert "6.3 mm" in prov.calls[0][1][0]["content"][0]["text"]


def test_ai_extract_only_keeps_template_keys():
    # An LLM that hallucinates an off-template key must not have it leak into dims.
    prov = _MockLLM('{"body_diameter_mm": 5, "body_length_mm": 10, "sneaky_key": 99}')
    out = ssm.ai_extract_dims(_CAP_DATASHEET, "capacitor_radial", provider=prov)
    assert "sneaky_key" not in out["dims"]
    assert set(out["dims"]) <= set(ssm.TEMPLATES["capacitor_radial"].keys)


def test_ai_extract_coerces_unit_strings():
    prov = _MockLLM('{"body_diameter_mm": "6.3 mm", "body_length_mm": "11.2mm"}')
    out = ssm.ai_extract_dims(_CAP_DATASHEET, "capacitor_radial", provider=prov)
    assert out["dims"]["body_diameter_mm"] == 6.3
    assert out["dims"]["body_length_mm"] == 11.2


def test_ai_extract_missing_required_reported_never_guessed():
    # Datasheet only gave diameter; length must be reported missing, not defaulted.
    prov = _MockLLM('{"body_diameter_mm": 6.3}')
    out = ssm.ai_extract_dims(_CAP_DATASHEET, "capacitor_radial", provider=prov)
    assert "body_length_mm" in out["missing_required"]
    assert "body_length_mm" not in out["dims"]


def test_ai_extract_unknown_template_raises():
    with pytest.raises(ValueError):
        ssm.ai_extract_dims(_CAP_DATASHEET, "not_a_template", provider=_MockLLM("{}"))


def test_default_provider_wraps_with_logging_ip16(monkeypatch):
    """The default provider is the configured T-F9 provider wrapped in the T-H7 LoggingProvider,
    tagged IP-16, emitting an ai_artifact event per call (flywheel-logging requirement)."""
    from services.agent.providers.logging_provider import LoggingProvider

    monkeypatch.setattr("services.agent.config.get_provider", lambda *a, **k: _MockLLM(_CAP_JSON))
    events: list[dict] = []
    prov = ssm._default_provider(events.append)
    assert isinstance(prov, LoggingProvider)
    # Drive one extraction through the wrapped provider.
    out = ssm.ai_extract_dims(_CAP_DATASHEET, "capacitor_radial", provider=prov)
    assert out["dims"]["body_diameter_mm"] == 6.3
    arts = [e for e in events if e.get("kind") == "ai_artifact"]
    assert arts, "expected an ai_artifact telemetry event from the logging provider"
    assert arts[0]["insertion_point"] == "IP-16"


def test_ai_extract_default_provider_emits_to_journal(monkeypatch, tmp_path):
    """With no on_event passed, the call still lands in the durable telemetry journal."""
    from engine import paths
    monkeypatch.setattr(paths, "CACHE", tmp_path)
    monkeypatch.setattr("services.agent.config.get_provider", lambda *a, **k: _MockLLM(_CAP_JSON))
    ssm.ai_extract_dims(_CAP_DATASHEET, "capacitor_radial")  # provider=None -> default path
    journal = tmp_path / "telemetry" / "events.jsonl"
    assert journal.exists()
    rows = [json.loads(ln) for ln in journal.read_text().splitlines() if ln.strip()]
    assert any(r.get("insertion_point") == "IP-16" for r in rows)


def _stub_build(template, dims, out_dir):
    p = out_dir / f"{template}.step"
    p.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
    return str(p)


def test_register_from_spec_sheet_creates_vendor_part(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    dims = {"body_diameter_mm": 6.3, "body_length_mm": 11.2, "capacitance_uf": 100}
    res = ssm.register_from_spec_sheet(
        dims, "capacitor_radial", "EEU-FR1V101",
        build_model=lambda t, d: _stub_build(t, d, tmp_path))
    assert res["registered"] is True
    assert res["source"] == "spec_sheet"
    vp = res["vendor_part"]
    assert vp["source"] == "spec_sheet"
    assert vp["part_number"] == "EEU-FR1V101"
    assert os.path.exists(vp["cad_path"])            # linked model doc
    assert vp["specs"]["template"] == "capacitor_radial"
    assert vp["specs"]["body_diameter_mm"] == 6.3
    assert "preview" in res and res["preview"]


def test_register_missing_required_raises(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        ssm.register_from_spec_sheet(
            {"body_diameter_mm": 6.3}, "capacitor_radial", "EEU-FR1V101",
            build_model=lambda t, d: _stub_build(t, d, tmp_path))


def test_register_requires_mpn(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        ssm.register_from_spec_sheet(
            {"body_diameter_mm": 6.3, "body_length_mm": 11.2}, "capacitor_radial", "  ")


def test_register_offline_no_model_degrades(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    res = ssm.register_from_spec_sheet(
        {"body_diameter_mm": 6.3, "body_length_mm": 11.2}, "capacitor_radial", "EEU-FR1V101",
        build_model=lambda t, d: None)   # core absent -> no model
    assert res["registered"] is False
    assert res["model_path"] is None
    assert res["vendor_part"] is None
    assert "reason" in res
    assert res["preview"]                # spec + preview still returned


def test_spec_sheet_match_full_path(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    res = ssm.spec_sheet_match(
        _CAP_DATASHEET, "EEU-FR1V101", "capacitor_radial",
        provider=_MockLLM(_CAP_JSON),
        build_model=lambda t, d: _stub_build(t, d, tmp_path))
    assert res["matched"] is True
    assert res["source"] == "spec_sheet"
    assert res["registered"] is True
    assert res["vendor_part"]["source"] == "spec_sheet"
    assert res["params"]["body_diameter_mm"] == 6.3
    assert res["preview"]


def test_spec_sheet_match_missing_dims_not_built(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    res = ssm.spec_sheet_match(
        _CAP_DATASHEET, "EEU-FR1V101", "capacitor_radial",
        provider=_MockLLM('{"body_diameter_mm": 6.3}'),   # length missing
        build_model=lambda t, d: _stub_build(t, d, tmp_path))
    assert res["matched"] is False
    assert "body_length_mm" in res["missing_required"]
    assert res["registered"] is False
    assert res["vendor_part"] is None


def test_list_templates_includes_capacitor():
    names = {t["name"] for t in ssm.list_templates()}
    assert "capacitor_radial" in names
    cap = next(t for t in ssm.list_templates() if t["name"] == "capacitor_radial")
    assert "body_diameter_mm" in cap["required"]


def test_gsl_preview_lists_dims():
    txt = ssm.gsl_preview({"body_diameter_mm": 6.3, "body_length_mm": 11.2},
                          "capacitor_radial", mpn="EEU-FR1V101")
    assert "EEU-FR1V101" in txt
    assert "body_diameter_mm" in txt


def test_spec_sheet_match_tool_registered():
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
    assert "spec_sheet_match" in m.tools
    assert "list_spec_templates" in m.tools
    out = json.loads(m.tools["list_spec_templates"]())
    assert out["ok"] is True
    assert any(t["name"] == "capacitor_radial" for t in out["templates"])
