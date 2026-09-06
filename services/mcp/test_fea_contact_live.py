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
    "held_by_contact_only", "pairs",
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


def _contact(tools: dict, doc: str, fz: float, vtu: str = None) -> dict:
    return json.loads(tools["run_fea_contact"](
        fixtures=["bottom"],
        load={"face": "top", "force": [0.0, 0.0, fz]},
        material="steel",
        max_gap_mm=0.2,
        mesh_size_mm=6.0,
        doc_id=doc,
        vtu_path=vtu,
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
