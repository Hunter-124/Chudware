"""Tests for the T-SIM3 sim_advisor CLI shim (GUI <-> T-W9 advisor seam).

The shim is a pure JSON passthrough over sim_advisor.precheck/postcheck plus the
optional ledger artifact event (inspectability rule). Tests run it exactly the
way the C++ GUI does — a subprocess with JSON on stdin — so the seam itself is
what's proven, not a python-level import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
CLI = Path(__file__).with_name("sim_advisor_cli.py")


def _run(req: dict, env_extra: dict | None = None) -> tuple[int, dict]:
    env = dict(os.environ)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        input=json.dumps(req).encode(),
        capture_output=True,
        cwd=str(REPO),
        env=env,
        timeout=60,
    )
    return proc.returncode, json.loads(proc.stdout.decode() or "{}")


class TestPrecheckMode:
    def test_unconstrained_study_reports_bc_items(self):
        rc, out = _run({"mode": "precheck",
                        "study": {"type": "static", "fixtures": [], "loads": [],
                                  "mesh_fineness": 0.15}},
                       {"CHUDWARE_SIM_PRECHECK_BLOCK": "0"})
        assert rc == 0 and out["ok"] is True
        checks = [(i["check"], i["severity"]) for i in out["items"]]
        assert ("sim_bc", "action") in checks          # no_fixture (block gate off)
        assert any(c == "sim_mesh" for c, _ in checks)  # coarse-mesh suggestion
        assert out["block_enabled"] is False

    def test_block_gate_passthrough(self):
        rc, out = _run({"mode": "precheck",
                        "study": {"type": "static", "fixtures": [], "loads": []}},
                       {"CHUDWARE_SIM_PRECHECK_BLOCK": "1"})
        assert rc == 0
        assert out["block_enabled"] is True
        assert any(i["severity"] == "blocker" for i in out["items"])

    def test_clean_study_only_anchor_info(self):
        rc, out = _run({"mode": "precheck",
                        "study": {"type": "static", "fixtures": ["f1"],
                                  "loads": [{"force": [0, 0, -100]}],
                                  "mesh_fineness": 0.5}})
        assert rc == 0
        assert [i["check"] for i in out["items"]] == ["sim_anchor"]


class TestPostcheckMode:
    def test_fail_verdict_and_narration(self):
        rc, out = _run({"mode": "postcheck",
                        "study": {"type": "static",
                                  "results": {"max_von_mises_mpa": 300.0},
                                  "material": {"name": "Steel AISI 1018",
                                               "yield_mpa": 370.0,
                                               "ultimate_mpa": 440.0}}})
        assert rc == 0 and out["ok"] is True
        assert out["verdict"] == "FAIL"
        assert out["mat_class"] == "metal_steel"
        assert out["sf_used"] == 1.5
        assert "FAIL" in out["narration"]

    def test_pass_verdict(self):
        rc, out = _run({"mode": "postcheck",
                        "study": {"type": "static",
                                  "results": {"max_von_mises_mpa": 100.0},
                                  "material": {"name": "Steel AISI 1018",
                                               "yield_mpa": 370.0,
                                               "ultimate_mpa": 440.0}}})
        assert rc == 0 and out["verdict"] == "PASS"

    def test_bad_mode_fails_cleanly(self):
        rc, out = _run({"mode": "nope", "study": {}})
        assert rc == 1 and out["ok"] is False and "nope" in out["error"]


class TestLedgerEvent:
    def test_event_written_when_run_id_present(self, tmp_path):
        # cache_root() honours the CHUDWARE_CACHE_ROOT test seam (services/agent/interaction.py).
        env = {"CHUDWARE_CACHE_ROOT": str(tmp_path)}
        rc, out = _run({"mode": "precheck",
                        "study": {"type": "static", "fixtures": [], "loads": []},
                        "ledger_run_id": "r_test_sim3", "study_id": "study-abc"},
                       env)
        assert rc == 0 and out["ok"] is True
        ledger = tmp_path / "runs" / "r_test_sim3" / "ledger.json"
        assert ledger.exists(), "ledger artifact event not written"
        # Events persist append-only in the ledger.jsonl sidecar (ledger.json holds
        # only the materialized state snapshot); one JSON event per line.
        sidecar = ledger.with_suffix(".jsonl")
        assert sidecar.exists(), "ledger.jsonl event sidecar not written"
        events = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
        evs = [e for e in events
               if e["actor"] == "gui_sim" and e["kind"] == "artifact"]
        assert evs, "no gui_sim artifact event"
        assert evs[-1]["payload"]["id"] == "study-abc"
        assert evs[-1]["payload"]["stage"] == "sim"
        assert "precheck" in evs[-1]["payload"]["note"]
