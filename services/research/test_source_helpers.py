"""Tests for the source-helper family in services.research (Pillar 8, contract
``contracts/research_tools.md``): unit_convert, eng_calc/check_stress, material_lookup,
thread_spec, standard_section, datasheet_fetch.

No network: every network-capable path (material_lookup's web_search fallback,
datasheet_fetch's http(s) branch) is either not exercised or force-offlined via a test
seam. Runnable via ``pytest``, matching the convention in
``services/research/test_research.py``.
"""
from __future__ import annotations

import importlib
import importlib.util

import pytest

from services import research
from services.research.eng_calc import check_stress
from services.research.unit_convert import normalizer_source
from services.net import safe_http

# Submodule object (not the package-level re-export) so we can monkeypatch its
# module-bound ``is_offline`` name — same pattern as test_research.py's _OfflineToggle.
material_lookup_mod = importlib.import_module("services.research.material_lookup")
datasheet_fetch_mod = importlib.import_module("services.research.datasheet_fetch")


# --------------------------------------------------------------------------- unit_convert


def test_unit_convert_inch_to_mm():
    out = research.unit_convert(1, "in", "length")
    assert out["ok"] is True
    assert out["value"] == 25.4
    assert out["units"] == "mm"
    assert out["confidence"] == "bundled"


def test_unit_convert_deg_is_identity():
    out = research.unit_convert(90, "deg", "angle")
    assert out["ok"] is True
    assert out["value"] == 90
    assert out["units"] == "deg"


def test_unit_convert_rejects_wrong_family():
    # reject-never-coerce: a mass unit is not a length unit, even though both are "units".
    out = research.unit_convert(1, "kg", "length")
    assert out["ok"] is False
    assert out["reason"] == "bad_unit"


def test_unit_convert_cm_to_mm():
    out = research.unit_convert(2, "cm", "length")
    assert out["ok"] is True
    assert out["value"] == 20.0


_AGENT_INTERACTION_AVAILABLE = importlib.util.find_spec("services.agent.interaction") is not None


@pytest.mark.skipif(
    not _AGENT_INTERACTION_AVAILABLE,
    reason="services.agent.interaction not present in this tree (pre T-F3 merge)",
)
def test_unit_convert_uses_canonical_normalizer_when_agent_interaction_lands():
    assert normalizer_source() == "services.agent.interaction"
    out = research.unit_convert(1, "in", "length")
    assert out["value"] == 25.4


def test_unit_convert_normalizer_source_is_a_known_seam():
    # Always-on counterpart to the skipif test above: whichever side of the T-F3 seam
    # we're on, the normalizer must be one of the two known sources.
    assert normalizer_source() in {"services.agent.interaction", "fallback"}


# --------------------------------------------------------------------------- eng_calc


def test_eng_calc_axial_stress():
    out = research.eng_calc("axial_stress", force_n=1000, area_mm2=100)
    assert out["ok"] is True
    assert out["value"] == 10
    assert out["units"] == "MPa"


def test_eng_calc_safety_factor():
    out = research.eng_calc("safety_factor", strength_mpa=250, stress_mpa=50)
    assert out["ok"] is True
    assert out["value"] == 5


def test_eng_calc_circle_area():
    out = research.eng_calc("circle_area", diameter_mm=10)
    assert out["ok"] is True
    assert out["value"] == pytest.approx(78.5398, rel=1e-4)


def test_eng_calc_unknown_formula():
    out = research.eng_calc("nope")
    assert out["ok"] is False
    assert out["reason"] == "not_found"


def test_eng_calc_missing_param():
    out = research.eng_calc("axial_stress", force_n=1000)
    assert out["ok"] is False
    assert out["reason"] == "bad_query"


# --------------------------------------------------------------------------- check_stress


def test_check_stress_ksi_to_mpa():
    out = check_stress(40, "ksi")
    assert out["ok"] is True
    assert out["value"] == pytest.approx(275.79, abs=0.1)
    assert out["units"] == "MPa"


def test_check_stress_flags_implausibly_low_mpa_as_likely_ksi():
    out = check_stress(5, "MPa", kind="yield")
    assert out["ok"] is True
    assert out["warning"] is not None


def test_check_stress_out_of_band_value_fails():
    out = check_stress(1e9, "MPa")
    assert out["ok"] is False
    assert out["reason"] == "bad_value"


def test_check_stress_plausible_mpa_has_no_warning():
    out = check_stress(200, "MPa")
    assert out["ok"] is True
    assert out["warning"] is None


# --------------------------------------------------------------------------- material_lookup


def test_material_lookup_6061_yield():
    out = research.material_lookup("6061", "yield")
    assert out["ok"] is True
    assert out["value"] == 276
    assert out["units"] == "MPa"
    assert out["confidence"] == "bundled"


def test_material_lookup_a36_density():
    # Registry alias is "a36" (see registry.json aliases), not the longer "A36 steel"
    # phrase — resolve_key lower-cases but does not fuzzy-match, so use the bundled alias.
    out = research.material_lookup("A36", "density")
    assert out["ok"] is True
    assert out["units"] == "g/cm^3"
    assert out["value"] == pytest.approx(7.85)


def test_material_lookup_titanium_full_property_set():
    out = research.material_lookup("titanium")
    assert out["ok"] is True
    assert out["confidence"] == "bundled"
    assert "properties" in out


def test_material_lookup_empty_query():
    out = research.material_lookup("")
    assert out["ok"] is False
    assert out["reason"] == "bad_query"


def test_material_lookup_miss_degrades_offline(monkeypatch):
    # Force the module's bound is_offline() True so an unknown material never reaches
    # web_search (keeps this test offline regardless of the ambient network state).
    monkeypatch.setattr(material_lookup_mod, "is_offline", lambda: True)
    out = research.material_lookup("unobtanium_xyz")
    assert out["ok"] is False


# --------------------------------------------------------------------------- thread_spec


def test_thread_spec_m8():
    out = research.thread_spec("M8")
    assert out["ok"] is True
    spec = out["spec"]
    assert spec["tensile_stress_area_mm2"] == pytest.approx(36.6087, abs=0.01)
    assert spec["pitch_diameter_mm"] == pytest.approx(7.1881, abs=0.001)
    assert out["confidence"] == "bundled"


def test_thread_spec_m8_explicit_pitch_matches_coarse():
    m8 = research.thread_spec("M8")
    m8x125 = research.thread_spec("M8x1.25")
    assert m8x125["ok"] is True
    assert m8x125["spec"]["tensile_stress_area_mm2"] == pytest.approx(
        m8["spec"]["tensile_stress_area_mm2"], abs=0.001
    )


def test_thread_spec_unc_quarter_20():
    out = research.thread_spec("1/4-20 UNC")
    assert out["ok"] is True
    assert out["spec"]["tensile_stress_area_in2"] == pytest.approx(0.0318, abs=0.0003)


def test_thread_spec_garbage():
    out = research.thread_spec("garbage")
    assert out["ok"] is False


# --------------------------------------------------------------------------- standard_section


def test_standard_section_w8x31():
    out = research.standard_section("W8x31")
    assert out["ok"] is True
    section = out["section"]
    assert section["Ix_in4"] == 110.0
    assert section["A_in2"] == 9.13
    assert out["confidence"] == "bundled"


def test_standard_section_pipe_2_sch_40():
    out = research.standard_section("pipe 2 sch 40")
    assert out["ok"] is True
    section = out["section"]
    assert section["ID_in"] == pytest.approx(2.067, abs=0.001)
    assert section["OD_in"] == 2.375


def test_standard_section_case_and_spacing_insensitive():
    out = research.standard_section("NPS 2 SCH40")
    assert out["ok"] is True


def test_standard_section_not_found():
    out = research.standard_section("nope")
    assert out["ok"] is False
    assert out["reason"] == "not_found"


# --------------------------------------------------------------------------- datasheet_fetch


def test_datasheet_fetch_empty_source():
    out = research.datasheet_fetch("")
    assert out["ok"] is False


def test_datasheet_fetch_missing_local_file():
    out = research.datasheet_fetch("/nonexistent_xyz.pdf")
    assert out["ok"] is False
    # "no_pdf_tool" if pdfminer.six isn't installed, "bad_url" if it is but the file
    # doesn't exist — either is a correct degradation depending on the environment.
    assert out["reason"] in {"no_pdf_tool", "bad_url"}


# ------------------------------------------------------- datasheet_fetch: T-X4 browser fallback

class _FakeHttpResponse:
    """Stands in for ``httpx.Response`` — just enough for datasheet_fetch's usage."""

    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture()
def fake_pdf_extractor(monkeypatch):
    """Force a working PDF extractor so these tests don't depend on pdfminer.six being
    installed — isolates the T-X4 fallback logic from the unrelated "no_pdf_tool"
    degradation already covered above."""
    monkeypatch.setattr(datasheet_fetch_mod, "_load_extractor",
                        lambda: (lambda data: "hello datasheet", "fake-extractor"))


def test_datasheet_fetch_plain_success_never_calls_browser_fallback(monkeypatch, fake_pdf_extractor):
    """DoD case 1: plain fetch succeeds -> browser path never invoked, zero behavior change."""
    from services.net import safe_http

    monkeypatch.setattr(safe_http, "get", lambda *a, **k: _FakeHttpResponse(200, b"%PDF-1.4 ..."))

    def _boom(url):
        raise AssertionError("browser fallback must not be invoked when the plain fetch succeeds")

    monkeypatch.setattr(datasheet_fetch_mod, "_try_browser_fallback", _boom)

    out = research.datasheet_fetch("https://example-vendor.com/widget.pdf")
    assert out["ok"] is True
    assert out["session_profile_used"] is None


def test_datasheet_fetch_403_with_profile_falls_back_to_browser(monkeypatch, fake_pdf_extractor):
    """DoD case 2: 403 + an existing matching profile -> browser fallback succeeds."""
    from services.net import safe_http
    from services.browser import profiles as browser_profiles
    from services.browser import session as browser_session

    monkeypatch.setattr(safe_http, "get", lambda *a, **k: _FakeHttpResponse(403))
    monkeypatch.setattr(browser_profiles, "has_profile", lambda site: site == "example-vendor.com")

    downloaded: dict = {}

    def _fake_browser_download(url, site, dest, **kwargs):
        dest.write_bytes(b"%PDF-1.4 fetched via browser session")
        downloaded["site"] = site
        return {"ok": True, "url": url, "session_profile_used": site}

    monkeypatch.setattr(browser_session, "browser_download", _fake_browser_download)

    out = research.datasheet_fetch("https://example-vendor.com/widget.pdf")
    assert out["ok"] is True
    assert out["session_profile_used"] == "example-vendor.com"
    assert downloaded["site"] == "example-vendor.com"


def test_datasheet_fetch_403_without_profile_degrades_unchanged(monkeypatch, fake_pdf_extractor):
    """DoD case 3: 403 + no saved profile -> existing degradation, unchanged (no login
    ever triggered from this path)."""
    from services.net import safe_http
    from services.browser import profiles as browser_profiles
    from services.browser import session as browser_session

    monkeypatch.setattr(safe_http, "get", lambda *a, **k: _FakeHttpResponse(403))
    monkeypatch.setattr(browser_profiles, "has_profile", lambda site: False)

    def _boom(*a, **k):
        raise AssertionError("browser_download must never run without an existing profile")

    monkeypatch.setattr(browser_session, "browser_download", _boom)

    out = research.datasheet_fetch("https://example-vendor.com/widget.pdf")
    assert out["ok"] is False
    assert out["reason"] == "network"


def test_datasheet_fetch_propagates_a_browser_cooldown_refusal(monkeypatch, fake_pdf_extractor):
    """A ``cooldown_active`` refusal must reach the tool boundary INTACT.

    Flattening it into ``network`` (which is what ``raise_for_status`` did a line
    later) told the model "could not fetch: HTTP 403" — so it retried, and every
    retry hit the same persisted per-site cooldown. The site's backoff can be up to
    900 s while this tool's harness budget is 180 s, so the only useful answer is the
    refusal itself plus ``retry_after_s``.
    """
    from services.net import safe_http
    from services.browser import session as browser_session

    monkeypatch.setattr(safe_http, "get", lambda *a, **k: _FakeHttpResponse(403))

    def _cooling(url, site, dest, **kwargs):
        return {"ok": False, "reason": "cooldown_active", "retry_after_s": 873.4,
                "policy_refusal": True, "executed": False,
                "hint": f"{site} is rate-limiting us; waiting inside this call is "
                        f"impossible — proceed with other sources."}

    monkeypatch.setattr(browser_session, "browser_download", _cooling)

    out = research.datasheet_fetch("https://example-vendor.com/widget.pdf")
    assert out["ok"] is False
    assert out["reason"] == "cooldown_active"      # NOT flattened to "network"
    assert out["retry_after_s"] == 873.4
    assert "other sources" in out["hint"]


def test_datasheet_fetch_other_browser_failures_still_degrade_as_network(
        monkeypatch, fake_pdf_extractor):
    """Only the actionable reasons are propagated; an ordinary browser failure keeps
    the pre-existing ``network`` degradation so nothing else changes shape."""
    from services.net import safe_http
    from services.browser import session as browser_session

    monkeypatch.setattr(safe_http, "get", lambda *a, **k: _FakeHttpResponse(403))
    monkeypatch.setattr(browser_session, "browser_download",
                        lambda url, site, dest, **k: {"ok": False,
                                                      "reason": "browser_download_failed",
                                                      "hint": "no body"})

    out = research.datasheet_fetch("https://example-vendor.com/widget.pdf")
    assert out["ok"] is False
    assert out["reason"] == "network"
