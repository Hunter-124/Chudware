"""run_fea_contact live regression — the MCP tool boundary over the real engine.

Everything below the tool (candidate pairing, the active-set penalty loop, the contact
report) is gated by the C++ ctest ``fea_contact`` on inline meshes. What is only
observable HERE is the tool's own contract: two boxes built with add_features, meshed by
the real mesher, solved through ``sim_run_static`` with ``study.contact``, and the packet
an agent actually reads back —

  * the load really crosses the closed 0.05 mm gap into the fixture
    (``reactions.equilibrium_error_pct`` under 1 %, ``contact.pairs[0].contact_force_n``
    equal to the applied force), and
  * reversing that load is REFUSED as a mechanism rather than reported as a solve.
  * a steel pin transfers a lateral load into a bronze bore, with each body's
    factor of safety measured against its own yield strength.
  * a bonded plate/bushing joint and a unilateral bushing/pin clearance coexist
    without fusing their material identities, and transmit bearing load into the plate.

Skips honestly when libchudware is not staged (same convention as the sibling live
suites); the services/mcp conftest gives the run its own private hostd.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.mcp import core_bridge as cb  # noqa: E402
from services.mcp.cad_tools import register_cad_tools  # noqa: E402

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; engine-backed MCP tools unavailable",
)

GAP_MM = 0.05
FORCE_N = 4000.0


class _CaptureMCP:
    """FastMCP stand-in that just collects the @mcp.tool() registrations."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def dec(fn):
            self.tools[kwargs.get("name", fn.__name__)] = fn
            return fn
        return dec


@pytest.fixture(scope="module")
def tools() -> dict:
    m = _CaptureMCP()
    register_cad_tools(m)
    return m.tools


def _stacked_boxes(tools: dict, name: str) -> str:
    """Two 20x20x10 mm boxes stacked along z with GAP_MM of air between them."""
    r = json.loads(tools["new_part"](name=name))
    assert r.get("ok"), r
    doc = r["doc_id"]
    rr = json.loads(tools["add_features"](doc_id=doc, ops=[
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Lower",
            "params": {"length": 20, "width": 20, "height": 10}}},
        {"op": "add_feature", "feature": {
            "type": "transform", "name": "Upper",
            "params": {"translate": [0.0, 0.0, 10.0 + GAP_MM], "copy": True}}},
    ]))
    assert rr.get("ok"), rr
    return doc


def _plate_bushing_pin(tools: dict, name: str) -> str:
    """Aluminium plate, touching bronze sleeve, and a steel pin with 0.05 mm clearance."""
    made = json.loads(tools["new_part"](name=name))
    assert made.get("ok"), made
    doc = made["doc_id"]
    built = json.loads(tools["add_features"](doc_id=doc, ops=[
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Plate", "centered": True,
            "params": {"length": 30, "width": 30, "height": 20}}},
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": "PlateBore", "mode": "cut", "centered": True,
            "params": {"radius": 7.5, "height": 40}}},
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": "Bushing", "centered": False, "merge": False,
            "at": [0, 0, -8], "params": {"radius": 7.5, "height": 16}}},
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": "BushingBore", "mode": "cut", "centered": True,
            "params": {"radius": 5.05, "height": 40}}},
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": "Pin", "centered": False, "merge": False,
            "at": [0, 0, -5], "params": {"radius": 5, "height": 20}}},
    ]))
    assert built.get("ok"), built
    return doc


def _contact_result(tools: dict, out: dict) -> dict:
    if out.get("job_id") and "provenance" not in out:
        job_id = out["job_id"]
        try:
            # A wait deadline ends only the wait, not the nonlinear solve.
            # These checks require terminal physics results, not a timing SLA.
            while True:
                status = json.loads(tools["wait_job"](job_id, timeout_s=900))
                if not status.get("still_running"):
                    return json.loads(tools["sim_results"](job_id))
        finally:
            status = json.loads(tools["sim_status"](job_id))
            if status.get("state") in ("queued", "running"):
                cancelled = json.loads(tools["sim_cancel"](job_id))
                assert cancelled.get("ok"), cancelled
    return out


def _contact(tools: dict, doc: str, fz: float, vtu: str = None,
             plasticity: dict = None) -> dict:
    return json.loads(tools["run_fea_contact"](
        fixtures=["bottom"],
        load={"face": "top", "force": [0.0, 0.0, fz]},
        material="steel",
        max_gap_mm=0.2,
        mesh_size_mm=6.0,
        doc_id=doc,
        vtu_path=vtu,
        plasticity=plasticity,
        closed_fit=False,  # This scenario measures the travel closing a real gap.
    ))


def test_compression_closes_the_gap_and_carries_the_load_into_the_fixture(tools, tmp_path):
    vtu = str(tmp_path / "contact.vtu")
    out = _contact(tools, _stacked_boxes(tools, "contact_live_press"), -FORCE_N, vtu)
    assert out.get("ok"), out.get("error")

    contact = out["contact"]
    assert contact["converged"] is True, contact
    assert contact["candidate_pairs"] > 0 and contact["active_pairs"] > 0, contact
    assert contact["contact_iterations"] >= 1, contact
    # Exactly one of the two bodies reaches the fixture only THROUGH the contact:
    # the box that is merely stacked on top (whichever ordinal the mesher gave it).
    assert len(contact["held_by_contact_only"]) == 1, contact
    # Frictionless: the tangential stabilizer must not be the load path.
    assert contact["tangential_force_fraction"] < 0.01, contact
    # ... and the interface IS the load path: the whole applied resultant on the
    # contact-held box crosses it, so the reported pressure is a seat pressure.
    assert contact["contact_carries_load"] is True, contact
    lp = contact["load_path"]
    assert abs(lp["applied_n"] - FORCE_N) < 0.01 * FORCE_N, lp
    assert abs(lp["fraction"] - 1.0) < 0.05, lp

    assert len(contact["pairs"]) == 1, contact["pairs"]
    pair = contact["pairs"][0]
    assert pair["bodies"] == [0, 1], pair
    # The whole applied force crosses the interface, and body 0 pushes body 1 UP.
    assert abs(pair["contact_force_n"] - FORCE_N) < 0.05 * FORCE_N, pair
    assert pair["force_vector_n"][2] > 0.0, pair
    assert pair["max_contact_pressure_mpa"] > 0.0, pair
    assert 0.0 <= pair["max_penetration_mm"] < 0.01, pair

    # ... and arrives at the fixture: equilibrium is the independent check.
    reactions = out["reactions"]
    assert reactions["equilibrium_error_pct"] < 1.0, reactions
    assert abs(reactions["reaction_force_n"][2] - FORCE_N) < 0.01 * FORCE_N, reactions
    # The gap really closed (0.05 mm of travel before any elastic squash).
    assert out["summary"]["max_disp_mm"] > GAP_MM, out["summary"]
    # The standard evidence envelope still rides along.
    assert out["mesh_quality"]["connected_components"] == 2, out["mesh_quality"]
    assert out["provenance"]["engine_build_id"], out["provenance"]
    assert out["vtu_path"] == vtu, out.get("vtu_path")
    assert Path(vtu).stat().st_size > 0, "contact run wrote no field file"


def test_pull_apart_is_refused_as_a_mechanism(tools):
    out = _contact_result(tools, _contact(
        tools, _stacked_boxes(tools, "contact_live_pull"), FORCE_N))
    assert out.get("ok") is False, out
    assert out["contact"]["load_path_refusal"]["criterion"] == "normal_cone_excludes_load", out
    assert out["mesh_quality"]["n_elems"] == 0, out


def test_steel_pin_in_bronze_bore_uses_each_bodys_yield(tools, tmp_path):
    made = json.loads(tools["new_part"](name="contact_live_steel_pin_bronze_bore"))
    assert made.get("ok"), made
    doc = made["doc_id"]
    built = json.loads(tools["add_features"](doc_id=doc, ops=[
        {"op": "add_feature", "feature": {
            "type": "box", "name": "BearingBlock", "centered": True,
            "params": {"length": 30, "width": 30, "height": 20}}},
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": "Bore", "mode": "cut", "centered": True,
            "params": {"radius": 5.2, "height": 40}}},
        {"op": "add_feature", "feature": {
            "type": "cylinder", "name": "Pin", "centered": False,
            "at": [0, 0, -5], "params": {"radius": 5, "height": 20}}},
    ]))
    assert built.get("ok"), built
    # STEP export traverses the block-with-bore first and the disconnected pin second.
    bore, pin = 1, 2
    bronze = {"name": "bronze", "E_mpa": 110000.0, "nu": 0.34, "yield_mpa": 125.0}
    out = json.loads(tools["run_fea_contact"](
        # The block bottom is z=-10; pin bottom -5 and top 15 isolate both selections.
        fixtures=["bottom"],
        load={"face": "top", "force": [FORCE_N, 0.0, 0.0]},
        material={"per_body": {str(bore): bronze, str(pin): "steel"}, "default": "steel"},
        max_gap_mm=0.4,
        max_iterations=100,
        mesh_size_mm=4.0,
        interface_size_mm=0.5,
        min_size_mm=0.5,
        doc_id=doc,
        vtu_path=str(tmp_path / "steel_pin_bronze_bore.vtu"),
    ))
    out = _contact_result(tools, out)
    assert out.get("ok"), json.dumps(out)
    assert out["provenance"]["material_mode"] == "per_body", out["provenance"]
    assert out["contact"]["converged"] is True, out["contact"]
    assert out["contact"]["contact_carries_load"] is True, out["contact"]
    assert out["reactions"]["equilibrium_error_pct"] < 1.0, out["reactions"]
    material = out["material"]
    summaries = material["body_summary"]
    bore_result = summaries[str(bore)]
    pin_result = summaries[str(pin)]
    bore_stress = bore_result["max_von_mises_mpa"]
    # Stay below the reporting cap: the assertion must discriminate bronze yield
    # from incorrectly using the steel pin's yield everywhere.
    assert bore_stress > bronze["yield_mpa"] / 15.0, bore_result
    assert bore_result["min_fos"] == pytest.approx(
        bronze["yield_mpa"] / bore_stress, rel=1e-5)
    steel_yield = material["per_body"][str(pin)]["yield_mpa"]
    assert steel_yield != bronze["yield_mpa"]
    assert bore_result["min_fos"] != pytest.approx(steel_yield / bore_stress, rel=0.01)
    assert out["summary"]["min_fos"] == pytest.approx(
        min(bore_result["min_fos"], pin_result["min_fos"]), abs=5e-5)
    from xml.etree import ElementTree
    grid = ElementTree.parse(out["vtu_path"])
    material_cells = grid.find(".//CellData/DataArray[@Name='material_id']")
    assert material_cells is not None
    assert {int(value) for value in material_cells.text.split()} == {
        material["per_body"][str(bore)]["material_id"],
        material["per_body"][str(pin)]["material_id"],
    }


def test_bonded_plate_bushing_and_contact_pin_transmit_bearing_load(tools, tmp_path):
    doc = _plate_bushing_pin(tools, "contact_live_mixed_bearing")
    bronze = {"name": "bronze", "E_mpa": 110000.0, "nu": 0.34, "yield_mpa": 125.0}
    penalty_scale = 0.5
    max_iterations = 100
    out = _contact_result(tools, json.loads(tools["run_fea_contact"](
        # Plate bottom z=-10 and pin top z=15 isolate the fixture and load.
        fixtures=["bottom"],
        load={"face": "top", "force": [FORCE_N, 0.0, 0.0]},
        material={"per_body": {"1": "aluminum", "2": bronze, "3": "steel"}},
        interfaces={"bonded": [[1, 2]], "contact": [[2, 3]], "default": "free"},
        bond_gap_mm=0.05,
        max_gap_mm=0.4,
        penalty_scale=penalty_scale,
        max_iterations=max_iterations,
        mesh_size_mm=4.0,
        interface_size_mm=0.5,
        min_size_mm=0.5,
        doc_id=doc,
        vtu_path=str(tmp_path / "plate_bushing_pin.vtu"),
    )))

    assert out.get("ok"), json.dumps(out)
    contact = out["contact"]
    assert contact["penalty_scale"] == pytest.approx(penalty_scale), contact
    assert contact["max_iterations"] == max_iterations, contact
    for kind, expected in (("bonded", (1, 2)), ("contact", (2, 3))):
        group = contact["interfaces"][kind]
        assert [(pair["a"], pair["b"]) for pair in group] == [expected], group
        assert group[0]["pairs"] > 0, group
        assert group[0]["area_mm2"] > 0.0, group
    assert contact["interfaces"]["unmatched"] == [], contact["interfaces"]
    assert contact["converged"] is True, contact
    assert contact["active_pairs"] > 0, contact
    assert contact["contact_carries_load"] is True, contact
    assert contact["tangential_force_fraction"] < 0.01, contact
    assert out["reactions"]["equilibrium_error_pct"] < 1.0, out["reactions"]
    assert out["reactions"]["reaction_force_n"][0] == pytest.approx(-FORCE_N, rel=0.01)
    # A fused plate/sleeve would erase the mixed-interface/material contract.
    assert out["mesh_quality"]["connected_components"] == 3, out["mesh_quality"]
    assert out["provenance"]["material_mode"] == "per_body", out["provenance"]
    material = out["material"]
    assert set(material["body_summary"]) == {"1", "2", "3"}, material
    assert material["per_body"]["2"]["yield_mpa"] == bronze["yield_mpa"], material
    assert material["per_body"]["1"]["material_id"] != material["per_body"]["2"]["material_id"]
    assert material["per_body"]["3"]["material_id"] != material["per_body"]["1"]["material_id"]


def test_deferred_unmatched_interface_refusal_keeps_diagnostics(tools):
    from services.mcp.cad_sim_tools import _build_contact_study
    from services.sim import jobs

    doc = _stacked_boxes(tools, "contact_unmatched_durable")
    ctx = _build_contact_study(
        ["bottom"], {"face": "top", "force": [0, 0, -FORCE_N]}, "steel",
        0.001, 25, 6.0, 1, doc, [],
        interfaces={"contact": [[1, 2]], "default": "free"},
    )
    # Submit the real solve durably instead of depending on wall-clock handoff
    # timing: a late FAILED result must preserve the same interface diagnosis.
    submitted = jobs.start("fea_contact", "core_rpc",
                           {"method": "sim_run_static", "params": {"study": ctx["study"]}})
    out = _contact_result(tools, submitted)
    assert out["ok"] is False and out["state"] == "failed", out
    assert out["contact"]["interfaces"]["unmatched"] == [
        {"a": 1, "b": 2, "type": "contact"}
    ], out
    assert out["mesh_quality"]["connected_components"] == 2, out
    assert "no fixture" in out["solver_error"], out
    assert "summary" not in out and "displacement" not in out, out


def test_per_body_refuses_an_ordinal_absent_from_the_mesh(tools):
    doc = _stacked_boxes(tools, "contact_material_missing_ordinal")
    out = json.loads(tools["run_fea_contact"](
        fixtures=["bottom"], load={"face": "top", "force": [0, 0, -FORCE_N]},
        material={"per_body": {"99": "steel"}, "default": "steel"},
        max_gap_mm=0.2, mesh_size_mm=6, doc_id=doc))
    assert out.get("ok") is False, out
    assert "ordinal 99" in out["error"] and "not in the mesh" in out["error"], out


def test_per_body_refuses_mixed_materials_after_body_fusion(tools):
    made = json.loads(tools["new_part"](name="contact_material_fused"))
    assert made.get("ok"), made
    doc = made["doc_id"]
    built = json.loads(tools["add_features"](doc_id=doc, ops=[
        {"op": "add_feature", "feature": {"type": "box",
         "params": {"length": 20, "width": 20, "height": 10}}},
        {"op": "add_feature", "feature": {"type": "transform",
         "params": {"translate": [0, 0, 5], "copy": True}}},
    ]))
    assert built.get("ok"), built
    out = json.loads(tools["run_fea_static"](
        fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -FORCE_N]}],
        material={"per_body": {"1": "steel", "2": "aluminum"}},
        mesh_size_mm=6, doc_id=doc))
    assert out.get("ok") is False, out
    assert "fused mesh group mixes materials" in out["error"], out
    assert "[1,2]" in out["error"], out


# ── material.plasticity across the MCP seam ─────────────────────────────────────
# The law itself is gated by ctest ``fea_plastic`` (bilinear closed form, collapse
# refusal, patch growth on a pin-on-edge bearing). What is only observable HERE is
# the seam: that ``plasticity=`` reaches the engine as ``study.material.plasticity``,
# that the packet an agent reads carries the block and the nodal field, that the .vtu
# gains the array, and that the two refusals arrive as refusals.
# A 20 x 20 mm seat carrying 4000 N is a 10 MPa nominal stress, so a 5 MPa yield puts
# the whole interface into the hardening branch at the load the elastic sibling test
# already uses. Real steel would need 100 kN for the same picture and a much finer
# mesh to resolve it; the law does not care which number is small.
SOFT_YIELD_MPA = 5.0
HARDENING_MPA = 200.0


def test_plasticity_caps_the_seat_stress_and_reports_its_state(tools, tmp_path):
    doc = _stacked_boxes(tools, "contact_live_plastic")
    elastic = _contact(tools, doc, -FORCE_N)
    assert elastic.get("ok"), elastic.get("error")
    assert "plasticity" not in elastic, "an elastic run must not claim a plastic state"

    vtu = str(tmp_path / "plastic.vtu")
    out = _contact(tools, doc, -FORCE_N, vtu, plasticity={
        "yield_mpa": SOFT_YIELD_MPA,
        "hardening_mpa": HARDENING_MPA,
        "max_increments": 6,
    })
    assert out.get("ok"), out.get("error")
    pl = out["plasticity"]
    assert pl["enabled"] is True and pl["converged"] is True, pl
    assert pl["yield_mpa"] == SOFT_YIELD_MPA and pl["hardening_mpa"] == HARDENING_MPA
    assert abs(pl["load_fraction"] - 1.0) < 1e-6, pl
    assert pl["requested_increments"] == 6, pl
    assert pl["increments"] >= 1 and pl["newton_iterations_total"] >= 1, pl
    # The seat really yielded, and the packet says how much of the part did.
    assert pl["max_equivalent_plastic_strain"] > 0.0, pl
    assert pl["yielded_nodes"] > 0, pl
    assert 0.0 < pl["plastic_fraction"] <= 1.0, pl
    assert pl["plastic_volume_mm3"] > 0.0, pl

    # The observable difference from the elastic run: the stress is BOUNDED by the flow
    # stress the law admits, where the elastic answer is not bounded by anything.
    flow = SOFT_YIELD_MPA + HARDENING_MPA * pl["max_equivalent_plastic_strain"]
    plastic_peak = out["summary"]["max_von_mises_raw_mpa"]
    elastic_peak = elastic["summary"]["max_von_mises_raw_mpa"]
    assert plastic_peak <= flow * 1.02, (plastic_peak, flow)
    assert plastic_peak < elastic_peak, (plastic_peak, elastic_peak)
    # Still a contact solve, still in equilibrium, and the load still crosses the seat.
    assert out["contact"]["converged"] is True, out["contact"]
    assert out["reactions"]["equilibrium_error_pct"] < 1.0, out["reactions"]
    assert abs(out["contact"]["pairs"][0]["contact_force_n"] - FORCE_N) < 0.05 * FORCE_N

    # The field that says WHERE it yielded rides in the .vtu. (Nodal arrays do not
    # cross this seam — the MCP envelope carries scalars and evidence blocks, and the
    # engine's own sim_run_static reply is where equivalent_plastic_strain[] lives.)
    assert out["vtu_path"] == vtu, out.get("vtu_path")
    assert "equivalent_plastic_strain" in Path(vtu).read_text()


def test_plasticity_past_the_limit_load_is_refused_with_the_load_fraction(tools):
    # Perfectly plastic (no hardening) at twice the section's limit load: there is no
    # equilibrium, so the run must refuse and say where it ran out.
    doc = _stacked_boxes(tools, "contact_live_collapse")
    out = _contact(tools, doc, -FORCE_N, plasticity={
        "yield_mpa": SOFT_YIELD_MPA, "hardening_mpa": 0.0, "max_increments": 6,
    })
    assert out.get("ok") is False, out
    err = out.get("error", "")
    assert "elastic-plastic collapse" in err, err
    # The refusal is QUANTITATIVE: it names the load fraction that was carried, which
    # for 4000 N over a 20 x 20 mm section at a 5 MPa yield is about half (limit load
    # 2000 N). Without that number a collapse refusal is not actionable.
    frac = float(re.search(r"no equilibrium exists past ([0-9.]+)%", err).group(1))
    assert 20.0 < frac < 80.0, err
    assert "summary" not in out, "a collapsed run must not report a stress state"


def test_plasticity_is_refused_where_it_would_be_ignored(tools):
    doc = _stacked_boxes(tools, "contact_live_plastic_refuse")
    # In the material dict it would be an elastic answer under a plastic label.
    out = json.loads(tools["run_fea_static"](
        fixtures=["bottom"],
        loads=[{"face": "top", "force": [0.0, 0.0, -FORCE_N]}],
        material={"E_mpa": 200000.0, "nu": 0.3, "yield_mpa": 250.0,
                  "plasticity": {"hardening_mpa": 1000.0}},
        doc_id=doc,
    ))
    assert out.get("ok") is False, out
    assert "not a material property" in out.get("error", ""), out.get("error")
    # And a modal study has no stress for a yield surface to bound.
    modal = cb.call("sim_run_modal", {
        "study": {
            "mesh": {"points": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
                     "tets": [[0, 1, 2, 3]]},
            "material": {"E_mpa": 200000.0, "nu": 0.3, "density_g_cm3": 7.85,
                         "plasticity": {"hardening_mpa": 1000.0}},
            "fixtures": [],
        },
        "n_modes": 1,
    })
    assert modal.get("ok") is False, modal
    assert "not applicable to a modal study" in modal.get("error", ""), modal


def test_real_stack_open_saddle_refuses_missing_normal_reaction(tools, tmp_path):
    import os
    import shutil
    source = os.environ.get("CHUDWARE_CONTACT_STACK")
    if not source:
        pytest.skip("set CHUDWARE_CONTACT_STACK to the private CC-006R stack fixture")
    path = tmp_path / "contact-stack.step+"
    shutil.copy2(source, path)
    opened = json.loads(tools["import_part"](path=str(path)))
    assert opened.get("ok"), opened
    started = time.monotonic()
    out = _contact_result(tools, json.loads(tools["run_fea_contact"](
        fixtures=["F12a36f"],
        load=[{"face": "Fb9679a", "force": [0, 0, -723]},
              {"face": "F0ec55c", "force": [0, 0, -723]}],
        material={"per_body": {
            "1": {"name": "6061-T6", "E_mpa": 68900, "nu": 0.33, "yield_mpa": 276},
            "2": {"name": "C93200", "E_mpa": 100000, "nu": 0.34, "yield_mpa": 125},
            "3": {"name": "4140", "E_mpa": 205000, "nu": 0.29, "yield_mpa": 655}}},
        interfaces={"bonded": [[1, 2]], "contact": [[2, 3]], "default": "free"},
        mesh_size_mm=4, min_size_mm=0.3, interface_size_mm=0.6,
        max_gap_mm=0.4, max_iterations=100, closed_fit=True,
        doc_id=opened["doc_id"],
    )))
    assert out.get("ok") is False, out
    contact = out["contact"]
    assert time.monotonic() - started < 120, out
    assert out["mesh_quality"]["n_elems"] == 0, out
    assert out["provenance"]["governor"]["probes"] == [], out
    refusal = contact["load_path_refusal"]
    assert refusal["criterion"] == "normal_cone_excludes_load", refusal
    assert refusal["bodies"] == [2, 3], refusal
    assert refusal["axis"] == "X", refusal
    assert refusal["normal_range"]["max"][0] < -0.6, refusal
    assert refusal["load_direction"] == pytest.approx([0, 0, -1]), refusal
    assert 80 < refusal["supported_arc_deg"] < 95, refusal
    assert "friction or an opposing bearing surface" in out.get("solver_error", out.get("error", "")), out
    assert "summary" not in out, out
    assert out["provenance"]["peak_rss_gb"] < 10, out
    print("REAL_STACK_REFUSAL", json.dumps({
        "elapsed_s": time.monotonic() - started, "refusal": refusal,
        "resolution": contact["resolution"], "n_elems": out["n_elems"],
        "mesh_quality": out["mesh_quality"], "provenance": out["provenance"]}))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
