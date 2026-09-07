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

Skips honestly when libchudware is not staged (same convention as the sibling live
suites); the services/mcp conftest gives the run its own private hostd.
"""
from __future__ import annotations

import json
import re
import sys
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
# The packet is a contract: an agent reading `contact` must find exactly these keys.
CONTACT_KEYS = {
    "enabled", "max_gap_mm", "penalty_scale", "penalty_stiffness_n_per_mm",
    "interface_size_mm", "max_iterations", "candidate_pairs", "active_pairs",
    "contact_iterations", "converged", "tangential_stabilization",
    "tangential_force_fraction", "candidate_area_mm2", "contact_area_mm2",
    "mean_contact_pressure_mpa", "contact_pressure_reliable", "resolution",
    "held_by_contact_only", "contact_carries_load", "load_path", "pairs",
}
PAIR_KEYS = {
    "bodies", "candidate_pairs", "active_pairs", "contact_force_n", "force_vector_n",
    "contact_area_mm2", "mean_contact_pressure_mpa", "max_contact_pressure_mpa",
    "max_penetration_mm",
}


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
    ))


def test_compression_closes_the_gap_and_carries_the_load_into_the_fixture(tools, tmp_path):
    vtu = str(tmp_path / "contact.vtu")
    out = _contact(tools, _stacked_boxes(tools, "contact_live_press"), -FORCE_N, vtu)
    assert out.get("ok"), out.get("error")

    contact = out["contact"]
    assert set(contact) == CONTACT_KEYS, sorted(set(contact) ^ CONTACT_KEYS)
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
    assert lp["note"] == "", lp

    assert len(contact["pairs"]) == 1, contact["pairs"]
    pair = contact["pairs"][0]
    assert set(pair) == PAIR_KEYS, sorted(set(pair) ^ PAIR_KEYS)
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
    out = _contact(tools, _stacked_boxes(tools, "contact_live_pull"), FORCE_N)
    assert out.get("ok") is False, out
    assert "contact separated" in out.get("error", ""), out


# ── material.plasticity across the MCP seam ─────────────────────────────────────
# The law itself is gated by ctest ``fea_plastic`` (bilinear closed form, collapse
# refusal, patch growth on a pin-on-edge bearing). What is only observable HERE is
# the seam: that ``plasticity=`` reaches the engine as ``study.material.plasticity``,
# that the packet an agent reads carries the block and the nodal field, that the .vtu
# gains the array, and that the two refusals arrive as refusals.
PLASTICITY_KEYS = {
    "enabled", "yield_mpa", "hardening_mpa", "tol", "requested_increments",
    "increments", "newton_iterations_total", "converged", "load_fraction",
    "max_equivalent_plastic_strain", "plastic_volume_mm3", "plastic_fraction",
    "yielded_nodes",
}
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
    assert set(pl) == PLASTICITY_KEYS, sorted(set(pl) ^ PLASTICITY_KEYS)
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
