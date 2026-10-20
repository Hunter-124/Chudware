"""Material-designation resolution regression suite (cad_common._resolve_material).

Measured live 2026-07-29 (claude-sonnet-4-5, structural FEA run): the model asked for
``material='A36 steel'``, was rejected with the 7-name listing error, and retried with plain
``'steel'``. A36 IS the registry's steel row — only the two-word spelling missed — so that
was a wasted round-trip on the single most common material name in structural analysis.

Groups:
  1. Designation resolution (no engine needed beyond the materials registry RPCs):
     aliases resolve to the SAME property dict as their canonical target, case/whitespace/
     hyphen normalization, registry-only alloys resolve to their OWN properties, and the
     refusal path for grades we recognise but deliberately will not substitute.
  2. One engine-backed static FEA proving 'A36 steel' and 'steel' produce the same
     deflection number (skipped cleanly on a sim-less build).

Run:  .venv-mcp/bin/python -X utf8 -m pytest services/mcp/test_material_resolve.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Repo root on sys.path regardless of symlink route (same fix as test_cad_tools.py).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from services.mcp import core_bridge as cb
from services.mcp.cad_common import _resolve_material

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; the materials registry RPCs are unavailable",
)

_MECH = ("E_mpa", "nu", "density_g_cm3", "yield_mpa")


# --------------------------------------------------------------- 1. designation resolution

def test_a36_steel_resolves_identically_to_steel():
    """The exact live failure: 'A36 steel' must be the steel row, property for property."""
    a36 = _resolve_material("A36 steel")
    steel = _resolve_material("steel")
    assert a36 == steel, f"A36 steel != steel: {a36} vs {steel}"
    # Prove it with the numbers the solver actually consumes, not just dict equality.
    assert [a36[f] for f in _MECH] == [steel[f] for f in _MECH]
    assert a36["E_mpa"] == 200000 and a36["yield_mpa"] == 250, a36


@pytest.mark.parametrize("spelling", [
    "A36 steel", "a36 steel", "  A36   STEEL  ", "A-36 Steel", "a36", "A36",
    "mild steel", "Mild Steel", "carbon steel",
])
def test_case_whitespace_and_hyphen_normalization(spelling):
    """Case, padding, collapsed whitespace and an in-designation hyphen all land on steel."""
    assert _resolve_material(spelling) == _resolve_material("steel"), spelling


@pytest.mark.parametrize("alias,canonical", [
    ("1018 steel", "1018"),            # registry row, reached through the category word
    ("304 stainless steel", "stainless"),
    ("18-8", "stainless"),             # 18Cr-8Ni fastener trade name == 304
    ("18-8 stainless steel", "stainless"),
    ("6061 aluminum", "aluminum"),
    ("6061-T6", "aluminum"),
    ("6061 t6 aluminium", "aluminum"),
    ("C360", "brass"),
    ("free-machining brass", "brass"),  # registry spells it "free-cutting brass"
    ("Ti-6Al-4V", "titanium"),
    ("TI 6AL 4V", "titanium"),
    ("grade 5 titanium", "titanium"),   # ASTM B265 Gr5 IS Ti-6Al-4V
    ("Ti Grade 5", "titanium"),
    ("PLA+", "pla"),
    ("PLA plastic", "pla"),
    ("ABS plastic", "abs"),
])
def test_alias_matches_its_target_material_exactly(alias, canonical):
    got = _resolve_material(alias)
    want = _resolve_material(canonical)
    assert got == want, f"{alias!r} -> {got} but {canonical!r} -> {want}"


@pytest.mark.parametrize("designation,name_frag,yield_mpa", [
    ("1018", "1018", 370),
    ("1018 steel", "1018", 370),
    ("4130 steel", "4130", 435),
    ("7075-T6", "7075", 503),
    ("7075 aluminum", "7075", 503),
    ("2024-T3", "2024", 345),
    ("titanium grade 2", "Grade 2", 275),
    ("cast iron", "Cast Iron", 130),
])
def test_registry_alloys_resolve_to_their_own_properties(designation, name_frag, yield_mpa):
    """Alloys outside the 7 fea_canonical rows are answered from the FULL registry, with
    their OWN first-party data — never aliased onto a generic. 7075-T6 is the load-bearing
    case: aliasing it to `aluminum` would hand the solver 276 MPa instead of 503 and halve
    the reported safety factor."""
    mat = _resolve_material(designation)
    assert name_frag.lower() in str(mat.get("name", "")).lower(), mat
    assert mat["yield_mpa"] == yield_mpa, mat
    assert mat["E_mpa"] > 0 and -1.0 < mat["nu"] < 0.5, mat


def test_7075_is_not_silently_generic_aluminum():
    """The anti-substitution invariant, stated as a number."""
    assert _resolve_material("7075-T6")["yield_mpa"] != _resolve_material("aluminum")["yield_mpa"]


@pytest.mark.parametrize("designation,label,nearest", [
    ("316", "Stainless 316", "stainless"),
    ("316 stainless steel", "Stainless 316", "stainless"),
    ("stainless 316L", "Stainless 316L", "stainless"),
    ("4140", "4140", "4130"),
    ("A572 Gr 50", "A572", "steel"),
    ("6063-T6", "6063", "aluminum"),
])
def test_unmodelled_grade_refuses_instead_of_substituting(designation, label, nearest):
    """A grade whose real yield differs materially from the nearest registry row must FAIL
    with paste-ready properties, never resolve to the near-miss row. A quietly wrong yield
    strength is worse than a clear error."""
    with pytest.raises(ValueError) as exc:
        _resolve_material(designation)
    msg = str(exc.value)
    assert label.lower() in msg.lower(), msg
    assert nearest in msg, msg
    assert "yield" in msg.lower() and "E_mpa" in msg, msg  # names the diff + the paste dict


def test_unknown_material_still_lists_the_known_set():
    """The existing honest, actionable error is preserved for a genuine unknown."""
    with pytest.raises(ValueError) as exc:
        _resolve_material("unobtanium-9000")
    msg = str(exc.value)
    assert msg.startswith("Unknown material 'unobtanium-9000'. Known: ["), msg
    for known in ("abs", "aluminum", "brass", "pla", "stainless", "steel", "titanium"):
        assert f"'{known}'" in msg, msg
    assert "explicit dict with E_mpa/nu/density_g_cm3/yield_mpa" in msg, msg


def test_explicit_dict_path_is_untouched():
    """An explicit properties dict is still returned as a COPY, unvalidated fields intact."""
    src = {"name": "custom", "E_mpa": 12345.0, "nu": 0.31, "density_g_cm3": 3.3,
           "yield_mpa": 99.0, "extra": "kept"}
    out = _resolve_material(src)
    assert out == src and out is not src
    out["E_mpa"] = 0.0
    assert src["E_mpa"] == 12345.0  # no aliasing back into the caller's dict


@pytest.mark.parametrize("bad,frag", [
    ({"E_mpa": 0.0, "nu": 0.3}, "E_mpa must be > 0"),
    ({"E_mpa": 200000.0, "nu": 0.9}, "nu must be in (-1, 0.5)"),
])
def test_explicit_dict_validation_still_fires(bad, frag):
    with pytest.raises(ValueError) as exc:
        _resolve_material(bad)
    assert frag in str(exc.value)


def test_resolved_dict_never_aliases_the_shared_cache():
    a = _resolve_material("A36 steel")
    a["E_mpa"] = 1.0
    assert _resolve_material("steel")["E_mpa"] == 200000


def test_resolved_shape_stays_the_flat_solver_shape():
    """The dict is handed to the C++ solver as study.material — a registry-sourced row must
    carry the same flat keys as a fea_table row, with no nested/extra fields."""
    canonical = set(_resolve_material("steel"))
    assert set(_resolve_material("7075-T6")) <= canonical, _resolve_material("7075-T6")
    assert "mechanical" not in _resolve_material("7075-T6")


def test_empty_and_none_still_default_to_steel():
    for blank in (None, "", "   "):
        assert _resolve_material(blank) == _resolve_material("steel"), repr(blank)


# ------------------------------------------------------------------ 2. engine-backed proof

_SIM_AVAILABLE: bool | None = None


def _require_sim() -> None:
    """Skip when the loaded engine was built without the FEA mesher."""
    global _SIM_AVAILABLE
    if _SIM_AVAILABLE is None:
        try:
            cb.call("sim_run_static", {})
            _SIM_AVAILABLE = True
        except cb.CoreError as exc:
            _SIM_AVAILABLE = "unknown method" not in str(exc).lower()
    if not _SIM_AVAILABLE:
        pytest.skip("engine has no FEA solver (CHUDWARE_WITH_POLYMESH=OFF)")


def _call(name: str, args: dict):
    from services.mcp import server

    _content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    try:
        return json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        return None


def _solve(name: str, args: dict, timeout_s: float = 300.0):
    r = _call(name, args)
    if not (isinstance(r, dict) and r.get("auto_backgrounded") and r.get("job_id")):
        return r
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        j = _call("get_job", {"job_id": r["job_id"]})
        if (j or {}).get("status") == "done":
            return j.get("result")
        if (j or {}).get("status") == "error":
            return {"ok": False, "error": (j or {}).get("error", "solve failed")}
        time.sleep(0.5)
    raise AssertionError(f"solve {name} did not finish within {timeout_s:.0f}s")


def test_fea_static_a36_steel_matches_steel_deflection():
    """The end-to-end number: the same cantilever solved as 'A36 steel' and as 'steel' must
    report the SAME max deflection, because both are the A36 row."""
    _require_sim()
    doc = _call("new_part", {"name": "a36_vs_steel"})["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        {"type": "box", "name": "Beam", "params": {"length": {"value": 80},
                                                   "width": {"value": 20},
                                                   "height": {"value": 10}}}]})
    common = {
        "doc_id": doc,
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -300]}],
    }
    r_alias = _solve("run_fea_static", {**common, "material": "A36 steel"})
    r_plain = _solve("run_fea_static", {**common, "material": "steel"})
    assert r_alias and r_alias.get("ok"), f"'A36 steel' solve failed: {r_alias}"
    assert r_plain and r_plain.get("ok"), f"'steel' solve failed: {r_plain}"
    d_alias = r_alias["summary"]["max_disp_mm"]
    d_plain = r_plain["summary"]["max_disp_mm"]
    assert d_plain > 0, r_plain
    assert d_alias == pytest.approx(d_plain, rel=1e-6), (
        f"'A36 steel' deflects {d_alias:.6f} mm but 'steel' deflects {d_plain:.6f} mm — "
        f"the alias resolved to a DIFFERENT material."
    )


if __name__ == "__main__":  # pragma: no cover - manual convenience
    sys.exit(pytest.main([__file__, "-v"]))


# ==========================================================================
# 3. APPEARANCE designation resolution (cad_common._resolve_appearance)
#
# A DIFFERENT surface from _resolve_material above: this one only picks a LOOK
# (colour/metallic/roughness), so the correct answer for an unmodelled alloy is the
# closest visual family, not a refusal. Measured live 2026-07-29 (claude-sonnet-4-5,
# "build a 60 mm cube in aluminium"): set_material('6061-T6') was refused with a list
# of eight appearance names, costing a call. Worse, 'Ti-6Al-4V' RESOLVED — to
# ALUMINIUM, because the string contains the token "al" — so titanium grade 5
# rendered as the wrong metal and reported success.
# ==========================================================================

from services.mcp import cad_common as _cc  # noqa: E402


def _appearance_family(name: str) -> str:
    """The preset NAME a spelling lands on, by matching the colour it returns."""
    got = _cc._resolve_appearance(name)
    for fam, preset in _cc._APPEARANCE_PRESETS.items():
        if list(preset["color"]) == list(got["color"]):
            return fam
    raise AssertionError(f"{name!r} produced an off-preset colour {got}")


@pytest.mark.parametrize("spelling,family", [
    # aluminium — bare designations, with and without temper
    ("6061-T6", "aluminum"), ("6061", "aluminum"), ("7075-T6", "aluminum"),
    ("2024-T3", "aluminum"), ("5052", "aluminum"),
    # steel — plate/bar grades and tool steel
    ("A36", "steel"), ("A36 steel", "steel"), ("1018", "steel"), ("4140", "steel"),
    ("12L14", "steel"), ("O1 tool steel", "steel"), ("mild steel", "steel"),
    # stainless — must beat plain "steel" on the substring
    ("304", "stainless"), ("316L", "stainless"), ("17-4PH", "stainless"),
    ("440C", "stainless"), ("stainless steel", "stainless"),
    # titanium — the silently-wrong case
    ("Ti-6Al-4V", "titanium"), ("Ti6Al4V", "titanium"), ("titanium", "titanium"),
    ("grade 5 titanium", "titanium"),
    # copper alloys and polymers
    ("C360", "brass"), ("C360 brass", "brass"), ("C110", "copper"),
    ("PETG", "plastic"), ("pla", "plastic"), ("ABS", "abs"), ("nylon", "nylon"),
    ("polyamide", "nylon"),
    # non-metals named in chat (live 2026-09-28: "birch plywood" failed outright)
    ("birch plywood", "wood"), ("3/4 in oak plywood", "wood"), ("pine", "wood"),
    ("walnut", "dark wood"), ("MDF", "mdf"), ("clear acrylic", "clear"),
    ("polycarbonate", "clear"), ("TPU", "rubber"), ("ASA", "plastic"),
])
def test_appearance_designation_lands_on_the_right_family(spelling, family):
    assert _appearance_family(spelling) == family


def test_titanium_grade_5_is_not_rendered_as_aluminium():
    """The regression that mattered: 'al' inside Ti-6Al-4V must not win.

    This asserts the DISTINCTION, not just the label — a wrong metal that returns
    ok:true is worse than a refusal, and only comparing the two colours catches it.
    """
    ti = _cc._resolve_appearance("Ti-6Al-4V")
    al = _cc._resolve_appearance("aluminum")
    assert ti["color"] != al["color"], "Ti-6Al-4V still resolves to aluminium"
    assert _appearance_family("Ti-6Al-4V") == "titanium"


def test_stainless_designation_is_not_plain_steel():
    """304/316 are visually distinct from carbon steel — keep them apart."""
    assert _cc._resolve_appearance("304")["color"] != _cc._resolve_appearance("A36")["color"]


@pytest.mark.parametrize("bogus", ["unobtainium", "XYZ-999", "kryptonite"])
def test_unknown_appearance_still_refuses_and_teaches(bogus):
    """The designation table must not become a catch-all that guesses a look."""
    with pytest.raises(ValueError) as exc:
        _cc._resolve_appearance(bogus)
    msg = str(exc.value)
    assert "Unknown material" in msg
    assert "engineering material registry" in msg  # names the resolution path it tried
    assert "alloy designations" in msg          # points at the new capability
    assert "'color'" in msg                     # and the explicit escape hatch


@pytest.mark.parametrize("spelling,key,yield_mpa,ultimate_mpa", [
    # ASTM/ASME minimums; spellings the piping/vessel benchmark models actually sent
    ("ASTM A106 Grade B", "steel_astm_a106_grade_b", 240, 415),
    ("A106-B", "steel_astm_a106_grade_b", 240, 415),
    ("SA-106-B", "steel_astm_a106_grade_b", 240, 415),
    ("A53 Gr B", "steel_astm_a53_grade_b", 240, 415),
    ("A53-B", "steel_astm_a53_grade_b", 240, 415),
    ("SA-516-70", "steel_asme_sa516_grade_70", 260, 485),
    ("SA-516 Gr 70", "steel_asme_sa516_grade_70", 260, 485),
    ("A516-70", "steel_asme_sa516_grade_70", 260, 485),
    ("A312 TP304", "stainless_astm_a312_tp304", 205, 515),
    ("A312-TP316", "stainless_astm_a312_tp316", 205, 515),
])
def test_piping_and_vessel_grades_resolve_identity_look_and_own_minima(
        spelling, key, yield_mpa, ultimate_mpa):
    """set_material resolves the look and the engineering key through the SAME registry row,
    and FEA gets that grade's own specified minima, not A36/304 by substitution."""
    look = _cc._resolve_appearance(spelling)
    assert len(look["color"]) == 3
    resolved = next(r for r in (cb.call("material_resolve_key", {"name": c}).get("result")
                                for c in _cc._material_candidates(spelling)) if r)
    assert (resolved.get("key") if isinstance(resolved, dict) else resolved) == key
    mat = _resolve_material(spelling)
    assert mat["yield_mpa"] == yield_mpa and mat["ultimate_mpa"] == ultimate_mpa, mat


@pytest.mark.parametrize("name", ["Inconel 718", "PEEK", "concrete", "tungsten",
                                  "Cast Iron (Gray)"])
def test_registry_rows_without_a_finish_still_get_a_look(name):
    """A registry material is never refused as 'unknown' just because it has no finish."""
    assert len(_cc._resolve_appearance(name)["color"]) == 3


def test_set_material_accepts_astm_pipe_and_vessel_grades_as_engineering_identity():
    """Benchmark F2/F3 regression: set_material refused 'ASTM A106 Grade B' and 'SA-516-70'
    as 'Unknown appearance material' although the piping tools use exactly those grades."""
    doc = _call("new_part", {"name": "astm_grade_identity", "fresh": True})["doc_id"]
    built = _call("add_features", {"doc_id": doc, "ops": [
        {"type": "box", "name": "Solid", "params": {"length": 10, "width": 10, "height": 10}}]})
    assert built["ok"], built
    for spelling, key in (("ASTM A106 Grade B", "steel_astm_a106_grade_b"),
                          ("SA-516-70", "steel_asme_sa516_grade_70")):
        assigned = _call("set_material", {"doc_id": doc, "material": spelling})
        assert assigned["ok"], assigned
        assert assigned["material_key"] == key, assigned
        assert assigned["material_registry_resolved"] is True, assigned


def test_explicit_colour_dict_still_bypasses_every_table():
    got = _cc._resolve_appearance({"color": [0.1, 0.2, 0.3], "metallic": 0.5})
    assert got["color"] == [0.1, 0.2, 0.3]
    assert got["metallic"] == 0.5


def test_token_scan_is_deterministic_and_base_metal_first(monkeypatch):
    """With the designation table gone, the LOOSE scan must still be right and stable.

    The candidate pool is built from a set, so two equal-length aliases ("al", "ti") had no
    defined order and 'Ti-6Al-4V' resolved to whichever the interpreter happened to visit
    first. Disabling the designation table isolates the scan; earliest-match precedence must
    pick the BASE metal (titanium) while 'Al' as the only family word still means aluminium.
    """
    monkeypatch.setattr(_cc, "_ALLOY_DESIGNATIONS", {})
    assert _appearance_family("Ti-6Al-4V") == "titanium"
    assert _appearance_family("6061-T6 Al") == "aluminum"
    assert _appearance_family("stainless steel") == "stainless"   # longest at same position
    assert _appearance_family("mild steel") == "steel"


def test_family_word_needs_a_whole_word_match(monkeypatch):
    """"al" must not fire inside an unrelated word — that is how loose scans go wrong."""
    monkeypatch.setattr(_cc, "_ALLOY_DESIGNATIONS", {})
    with pytest.raises(ValueError):
        _cc._resolve_appearance("alloy of unknown type")


# ── Material dict key spelling (2026-08-04) ──────────────────────────────────
#
# The schema is {E_mpa, nu, density_g_cm3, yield_mpa} — one capitalized key
# among lowercase siblings. A model writing the obvious `e_mpa` was told
# "E_mpa must be > 0 (got 0.0)" about a dict that was entirely correct.
# Observed live (k3-256k, T-W11 bracket):
#   {"name": "6061-T6", "e_mpa": 68900, "nu": 0.33, "yield_mpa": 276}


def test_lowercase_e_mpa_is_accepted():
    from services.mcp.cad_common import _resolve_material
    mat = _resolve_material(
        {"name": "6061-T6", "e_mpa": 68900, "nu": 0.33, "yield_mpa": 276})
    assert mat["E_mpa"] == 68900
    assert mat["nu"] == 0.33
    assert mat["yield_mpa"] == 276
    assert mat["name"] == "6061-T6"          # unknown keys pass through


def test_longform_property_aliases_are_accepted():
    from services.mcp.cad_common import _resolve_material
    mat = _resolve_material({"youngs_modulus_mpa": 70000,
                             "poissons_ratio": 0.33, "density": 2.7})
    assert mat["E_mpa"] == 70000
    assert mat["nu"] == 0.33
    assert mat["density_g_cm3"] == 2.7


def test_canonical_key_wins_over_an_alias():
    """A dict carrying both spellings must never silently flip to the alias."""
    from services.mcp.cad_common import _resolve_material
    mat = _resolve_material({"E_mpa": 70000, "e_mpa": 1.0, "nu": 0.33})
    assert mat["E_mpa"] == 70000


def test_a_genuinely_zero_modulus_is_still_rejected():
    """Canonicalizing keys must not soften the physical validation."""
    from services.mcp.cad_common import _resolve_material
    with pytest.raises(ValueError, match="E_mpa must be > 0"):
        _resolve_material({"e_mpa": 0, "nu": 0.33})


def test_density_only_tread_measures_mass_but_refuses_isotropic_fea():
    """Published filament density is sufficient for mass, never a substitute for a model."""
    material = "tpu_ultimaker_s95a_black"
    doc = _call("new_part", {"name": "density_only_tread", "fresh": True})["doc_id"]
    built = _call("add_features", {"doc_id": doc, "ops": [
        {"type": "box", "name": "Solid", "params": {
            "length": 10, "width": 10, "height": 10}}]})
    assert built["ok"], built
    assigned = _call("set_material", {"doc_id": doc, "material": material})
    assert assigned["ok"], assigned
    mass = _call("measure_part", {"doc_id": doc, "material": material})
    assert mass["mass"]["mass_g"] == pytest.approx(1.22, rel=1e-6), mass
    explicit = _call("measure_part", {
        "doc_id": doc, "material": material, "density_g_cm3": 2.7})
    assert explicit["mass"]["mass_g"] == pytest.approx(2.7, rel=1e-6), explicit
    applied = _call("measure_part", {"doc_id": doc})  # the material set above
    assert applied["mass"]["mass_g"] == pytest.approx(1.22, rel=1e-6), applied
    with pytest.raises(ValueError):
        _resolve_material(material)


def test_plywood_density_does_not_authorize_isotropic_elasticity():
    assert _cc._resolve_material_density("finnish_birch_plywood_9mm_7ply") == 0.68
    with pytest.raises(ValueError):
        _resolve_material("finnish_birch_plywood_9mm_7ply")
    # Even complete-looking scalar properties cannot erase an explicit orthotropic model.
    with pytest.raises(ValueError):
        _resolve_material({"constitutive_model": "orthotropic", "E_mpa": 10000,
                           "nu": 0.3, "yield_mpa": 30, "density_g_cm3": 0.68})


def test_selected_section_strengths_do_not_fall_back_to_generic_alloys():
    shaft = _resolve_material("42CrMo4+QT 750")
    hub = _resolve_material("42CrMo4+QT 650")
    plate = _resolve_material("6061-T6 unwelded plate 240")
    assert shaft["yield_mpa"] == 750
    assert hub["yield_mpa"] == 650
    assert plate["yield_mpa"] == 240
    assert _resolve_material("6061-T6")["yield_mpa"] == 276
    with pytest.raises(ValueError):
        _resolve_material("4140")


def test_product_registry_appearance_overrides_generic_family_and_is_copied():
    # "natural" would otherwise lose to generic "plastic"; product black is not silver.
    natural = _cc._resolve_appearance("Ensinger TECAFORM AH natural POM-C")
    black = _cc._resolve_appearance("UltiMaker S-series TPU 95A black")
    assert natural["metallic"] == black["metallic"] == 0
    assert natural["color"] == [0.9, 0.89, 0.85]
    assert black["color"] == [0.045, 0.045, 0.05]
    natural["color"][0] = 0
    assert _cc._resolve_appearance("TECAFORM AH natural")["color"][0] == 0.9
