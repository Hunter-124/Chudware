"""Tests for engine_oracle/vision/dense_backends.py.

These tests PASS without OpenMVS or Brush installed.  They verify:
  1. Correct command-line construction (arg order, I/O paths).
  2. Graceful backend_not_provisioned envelope when binaries are absent.
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure the vision package is importable when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine_oracle.vision.dense_backends import (
    brush_commands,
    openmvs_commands,
    run_brush,
    run_openmvs,
)

SPARSE = "/session/sparse/0"
SESSION = "/session"
DENSE_DIR = "/session/dense"
MESH_DIR = "/session/mesh"
SPLAT_DIR = "/session/splat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_bins(monkeypatch):
    monkeypatch.delenv("OPENMVS_BIN", raising=False)
    monkeypatch.delenv("BRUSH_BIN", raising=False)


def _set_openmvs(monkeypatch, path="/fake/openmvs/bin"):
    monkeypatch.setenv("OPENMVS_BIN", path)


def _set_brush(monkeypatch, path="/fake/brush"):
    monkeypatch.setenv("BRUSH_BIN", path)


# ---------------------------------------------------------------------------
# backend_not_provisioned — no binaries installed
# ---------------------------------------------------------------------------

class TestNotProvisioned:
    def test_openmvs_commands_not_provisioned(self, monkeypatch):
        _clear_bins(monkeypatch)
        result = openmvs_commands(SPARSE, SESSION)
        assert result["ok"] is False
        assert result["reason"] == "backend_not_provisioned"
        assert result["backend"] == "openmvs"
        assert "OPENMVS_BIN" in result["hint"]

    def test_run_openmvs_not_provisioned(self, monkeypatch):
        _clear_bins(monkeypatch)
        result = run_openmvs(SPARSE, SESSION)
        assert result["ok"] is False
        assert result["reason"] == "backend_not_provisioned"
        assert result["backend"] == "openmvs"

    def test_brush_commands_not_provisioned(self, monkeypatch):
        _clear_bins(monkeypatch)
        result = brush_commands(SPARSE, SESSION)
        assert result["ok"] is False
        assert result["reason"] == "backend_not_provisioned"
        assert result["backend"] == "brush"
        assert "BRUSH_BIN" in result["hint"]

    def test_run_brush_not_provisioned(self, monkeypatch):
        _clear_bins(monkeypatch)
        result = run_brush(SPARSE, SESSION)
        assert result["ok"] is False
        assert result["reason"] == "backend_not_provisioned"
        assert result["backend"] == "brush"


# ---------------------------------------------------------------------------
# OpenMVS command-line construction
# ---------------------------------------------------------------------------

class TestOpenMVSCommands:
    BIN = "/opt/openmvs/bin"

    def setup_method(self):
        os.environ["OPENMVS_BIN"] = self.BIN

    def teardown_method(self):
        os.environ.pop("OPENMVS_BIN", None)

    def test_returns_ok(self):
        result = openmvs_commands(SPARSE, SESSION)
        assert result["ok"] is True
        assert result["backend"] == "openmvs"

    def test_five_commands(self):
        result = openmvs_commands(SPARSE, SESSION)
        assert len(result["commands"]) == 5

    def test_interface_colmap_first(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        interface = cmds[0]
        assert interface[0].endswith("InterfaceCOLMAP")
        # --input-path points to sparse_dir
        idx = interface.index("--input-path")
        assert interface[idx + 1] == SPARSE
        # --output-file ends in dense.mvs inside dense/
        idx = interface.index("--output-file")
        assert interface[idx + 1].startswith(DENSE_DIR)
        assert interface[idx + 1].endswith(".mvs")

    def test_densify_second(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        densify = cmds[1]
        assert densify[0].endswith("DensifyPointCloud")
        # input-file is the .mvs from step 1
        in_idx = densify.index("--input-file")
        assert densify[in_idx + 1] == cmds[0][cmds[0].index("--output-file") + 1]
        # output-file is a .ply
        out_idx = densify.index("--output-file")
        assert densify[out_idx + 1].endswith(".ply")

    def test_reconstruct_mesh_third(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        recon = cmds[2]
        assert recon[0].endswith("ReconstructMesh")
        in_idx = recon.index("--input-file")
        # input is the dense ply from densify
        assert recon[in_idx + 1] == cmds[1][cmds[1].index("--output-file") + 1]

    def test_refine_mesh_fourth(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        refine = cmds[3]
        assert refine[0].endswith("RefineMesh")
        in_idx = refine.index("--input-file")
        assert refine[in_idx + 1] == cmds[2][cmds[2].index("--output-file") + 1]

    def test_texture_mesh_fifth_outputs_to_mesh_dir(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        texture = cmds[4]
        assert texture[0].endswith("TextureMesh")
        out_idx = texture.index("--output-file")
        assert texture[out_idx + 1].startswith(MESH_DIR)
        assert texture[out_idx + 1].endswith(".obj")

    def test_texture_mesh_export_type_obj(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        texture = cmds[4]
        et_idx = texture.index("--export-type")
        assert texture[et_idx + 1] == "obj"

    def test_working_resolution_default_zero(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        densify = cmds[1]
        wr_idx = densify.index("--working-resolution")
        assert densify[wr_idx + 1] == "0"

    def test_working_resolution_custom(self):
        cmds = openmvs_commands(SPARSE, SESSION, working_resolution=4)["commands"]
        densify = cmds[1]
        wr_idx = densify.index("--working-resolution")
        assert densify[wr_idx + 1] == "4"

    def test_all_bins_under_bin_dir(self):
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        for cmd in cmds:
            assert cmd[0].startswith(self.BIN), f"{cmd[0]} not under {self.BIN}"

    def test_i_o_chaining(self):
        """Each step's output-file is the next step's input-file."""
        cmds = openmvs_commands(SPARSE, SESSION)["commands"]
        for i in range(1, len(cmds) - 1):
            prev_out = cmds[i - 1][cmds[i - 1].index("--output-file") + 1]
            this_in = cmds[i][cmds[i].index("--input-file") + 1]
            assert prev_out == this_in, f"Chain broken between step {i-1} and {i}"


# ---------------------------------------------------------------------------
# Brush command-line construction
# ---------------------------------------------------------------------------

class TestBrushCommands:
    BIN = "/usr/local/bin/brush"

    def setup_method(self):
        os.environ["BRUSH_BIN"] = self.BIN

    def teardown_method(self):
        os.environ.pop("BRUSH_BIN", None)

    def test_returns_ok(self):
        result = brush_commands(SPARSE, SESSION)
        assert result["ok"] is True
        assert result["backend"] == "brush"

    def test_two_commands_by_default(self):
        result = brush_commands(SPARSE, SESSION)
        assert len(result["commands"]) == 2

    def test_one_command_when_no_mesh_export(self):
        result = brush_commands(SPARSE, SESSION, export_mesh=False)
        assert len(result["commands"]) == 1

    def test_train_command_structure(self):
        cmds = brush_commands(SPARSE, SESSION)["commands"]
        train = cmds[0]
        assert train[0] == self.BIN
        assert train[1] == "train"
        sp_idx = train.index("--source-path")
        assert train[sp_idx + 1] == SPARSE
        mp_idx = train.index("--model-path")
        assert train[mp_idx + 1].startswith(SPLAT_DIR)
        ply_idx = train.index("--output-ply")
        assert train[ply_idx + 1].endswith(".ply")
        iter_idx = train.index("--iterations")
        assert train[iter_idx + 1] == "30000"

    def test_train_custom_steps(self):
        cmds = brush_commands(SPARSE, SESSION, max_steps=10_000)["commands"]
        train = cmds[0]
        iter_idx = train.index("--iterations")
        assert train[iter_idx + 1] == "10000"

    def test_export_mesh_command_structure(self):
        cmds = brush_commands(SPARSE, SESSION)["commands"]
        export = cmds[1]
        assert export[0] == self.BIN
        assert export[1] == "export-mesh"
        # --splat must match the train --output-ply
        train_ply = cmds[0][cmds[0].index("--output-ply") + 1]
        splat_idx = export.index("--splat")
        assert export[splat_idx + 1] == train_ply
        # --output goes to mesh dir
        out_idx = export.index("--output")
        assert export[out_idx + 1].startswith(MESH_DIR)
        # method is tsdf
        m_idx = export.index("--method")
        assert export[m_idx + 1] == "tsdf"

    def test_brush_bin_in_all_commands(self):
        cmds = brush_commands(SPARSE, SESSION)["commands"]
        for cmd in cmds:
            assert cmd[0] == self.BIN
