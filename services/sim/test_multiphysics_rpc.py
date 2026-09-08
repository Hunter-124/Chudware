from __future__ import annotations

import pytest

from services.mcp import fea_advanced as fa
from services.sim import multiphysics_rpc as mp


@pytest.mark.parametrize("endpoint, params, expected", [
    (mp._sim_run_contact, {"pressure_mpa": 0.0}, "pressure_mpa"),
    (mp._sim_run_contact, {"nu": 0.5}, "nu"),
])
def test_malformed_proxy_studies_fail_before_backend_probe(
        monkeypatch, endpoint, params, expected):
    probed = False

    def backend_probe():
        nonlocal probed
        probed = True
        return None

    monkeypatch.setattr(fa, "ccx_path", backend_probe)
    result = endpoint(params)
    assert result["ok"] is False
    assert expected in result["error"]
    assert probed is False


def _patch_contact_backend(monkeypatch, reaction_per_node):
    monkeypatch.setattr(fa, "ccx_path", lambda: "/fake/ccx")
    monkeypatch.setattr(fa, "build_contact_block_inp", lambda *_args, **_kwargs: {
        "inp": "deck", "base_nodes": [1, 2, 3, 4], "applied_load_n": 100.0,
    })
    monkeypatch.setattr(fa, "run_ccx", lambda *_args, **_kwargs: {
        "ok": True, "dat_path": "/fake/result.dat", "backend": "ccx",
    })
    monkeypatch.setattr(
        fa, "parse_dat_nodal", lambda *_args, **_kwargs: reaction_per_node)


def test_contact_rejects_missing_reaction_field(monkeypatch):
    _patch_contact_backend(monkeypatch, None)
    result = mp._sim_run_contact({})
    assert result["ok"] is False
    assert "no finite base reaction" in result["error"]


def test_contact_rejects_unbalanced_reaction(monkeypatch):
    _patch_contact_backend(monkeypatch, 10.0)
    result = mp._sim_run_contact({})
    assert result["ok"] is False
    assert result["reaction_balance_error_pct"] == pytest.approx(60.0)
    assert "force balance" in result["error"]


def test_worker_wrapper_rejects_nonfinite_or_non_object_backend_results():
    nonfinite = mp._wrap(lambda _params: {"ok": True, "value": float("nan")})
    not_an_object = mp._wrap(lambda _params: None)
    result = nonfinite({}, lambda *_args: None)
    assert result["ok"] is False
    assert "Out of range float" in result["error"]
    result = not_an_object({}, lambda *_args: None)
    assert result["ok"] is False
    assert "non-object" in result["error"]


def test_ecad_handoff_rejects_zero_mesh_before_dispatch():
    result = mp.WORKER_METHODS["ecad_thermal_handoff"](
        {"mesh_fineness": 0.0}, lambda *_args: None)
    assert result["ok"] is False
    assert "mesh_fineness" in result["error"]


def test_heatsink_dispatch_rejects_non_object_result(monkeypatch):
    monkeypatch.setattr(mp, "_heatsink_endpoint", lambda: (lambda _params: None))
    result = mp._heatsink_select({})
    assert result["ok"] is False
    assert "non-object" in result["error"]
