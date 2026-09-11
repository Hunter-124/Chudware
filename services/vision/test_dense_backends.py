"""Tests for services/vision/dense_backends.py.

Tests run without OpenMVS or Brush installed. Brush subprocess doubles exercise
observable failure, complete-output publication, and cancellation boundaries.
"""
from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

# Ensure the vision package is importable when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ..vision.dense_backends import (
    openmvs_commands,
    run_brush,
    run_openmvs,
)

SPARSE = "/session/sparse/0"
SESSION = "/session"
DENSE_DIR = "/session/dense"
MESH_DIR = "/session/mesh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_bins(monkeypatch):
    monkeypatch.delenv("OPENMVS_BIN", raising=False)
    monkeypatch.delenv("BRUSH_BIN", raising=False)


def _set_openmvs(monkeypatch, path="/fake/openmvs/bin"):
    monkeypatch.setenv("OPENMVS_BIN", path)




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

    def test_run_brush_not_provisioned(self, monkeypatch, tmp_path):
        _clear_bins(monkeypatch)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
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


@pytest.fixture
def brush_dataset(tmp_path):
    dataset = tmp_path / "dataset"
    model = dataset / "sparse/0"
    model.mkdir(parents=True)
    (dataset / "images").mkdir()
    for name in ("cameras", "images", "points3D"):
        (model / (name + ".txt")).write_text("# model\n")
    return model, tmp_path / "session", dataset


@pytest.fixture
def brush_process(tmp_path, monkeypatch):
    """Executable double produces real scalar Gaussian PLY, never engine fixtures."""
    executable = tmp_path / "brush"
    executable.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""
        import os, pathlib, struct, sys, time
        args = sys.argv
        out = pathlib.Path(args[args.index("--export-path") + 1])
        steps = int(args[args.index("--total-steps") + 1])
        mode = os.environ.get("BRUSH_TEST_MODE", "valid")
        props = ["x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2",
                 "rot_0", "rot_1", "rot_2", "rot_3", "f_dc_0", "f_dc_1", "f_dc_2"]
        header = ("ply\\nformat binary_little_endian 1.0\\nelement vertex 1\\n" +
                  "".join("property float " + p + "\\n" for p in props) + "end_header\\n").encode()
        payload = struct.pack("<14f", 1, 2, 3, 0, -1, -2, -3, 1, 0, 0, 0, .2, .4, .6)
        path = out / ("export_%d.ply" % steps)
        if mode == "missing":
            sys.exit(0)
        if mode == "cancel":
            pathlib.Path(os.environ["BRUSH_TEST_PID"]).write_text(str(os.getpid()))
            time.sleep(60)
        if mode != "progress":
            path.write_bytes(header if mode == "truncated" else header + payload)
        if mode == "failed":
            print("GPU initialization failed", flush=True)
            sys.exit(7)
        if mode == "progress":
            intermediate = out / "export_1.ply"
            intermediate.write_bytes(header)
            time.sleep(1.3)
            intermediate.write_bytes(header + payload)
            time.sleep(1.3)
            path.write_bytes(header + payload)
    """))
    executable.chmod(0o755)
    monkeypatch.setenv("BRUSH_BIN", str(executable))
    return executable


def test_brush_rejects_invalid_override_even_with_managed_install(tmp_path, monkeypatch):
    from .reveng_provision import brush_install_dir, brush_executable
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    managed = brush_install_dir() / "brush_app"
    managed.parent.mkdir(parents=True)
    managed.write_text("#!/bin/sh\nexit 0\n")
    managed.chmod(0o755)
    assert brush_executable() == str(managed)
    monkeypatch.setenv("BRUSH_BIN", str(tmp_path / "missing"))
    assert brush_executable() is None


@pytest.mark.parametrize("mode,reason", [
    ("missing", "invalid_brush_output"),
    ("truncated", "invalid_brush_output"),
    ("failed", "brush_failed"),
])
def test_brush_does_not_report_false_success(brush_dataset, brush_process, monkeypatch, mode, reason):
    model, session, dataset = brush_dataset
    monkeypatch.setenv("BRUSH_TEST_MODE", mode)
    result = run_brush(str(model), str(session), image_dir=str(dataset), max_steps=2)
    assert result["ok"] is False
    assert result["reason"] == reason
    if mode == "failed":
        assert result["returncode"] == 7
        assert "GPU initialization failed" in result["hint"]


def test_brush_publishes_complete_immutable_output(brush_dataset, brush_process, monkeypatch):
    from .gaussian_pipeline import inspect_splat
    model, session, dataset = brush_dataset
    monkeypatch.setenv("BRUSH_TEST_MODE", "progress")
    snapshots = {}

    def progress(stage, pct, msg, artifact=None):
        if artifact:
            path = Path(artifact["splat_path"])
            assert inspect_splat(str(path))["count"] == 1
            data = path.read_bytes()
            assert snapshots.get(path, data) == data
            snapshots[path] = data

    result = run_brush(str(model), str(session), image_dir=str(dataset),
                       max_steps=2, export_every=1, progress=progress)
    assert result["ok"] is True
    assert result["iteration"] == 2
    assert result["bounds"] == [1, 2, 3, 1, 2, 3]
    assert result["mesh_path"] is None
    assert {p.name for p in snapshots} == {"export_1.ply", "export_2.ply"}
    for path, data in snapshots.items():
        assert path.read_bytes() == data


def test_brush_cancel_reaps_process(brush_dataset, brush_process, monkeypatch, tmp_path):
    model, session, dataset = brush_dataset
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("BRUSH_TEST_MODE", "cancel")
    monkeypatch.setenv("BRUSH_TEST_PID", str(pid_file))
    start = time.monotonic()
    result = run_brush(str(model), str(session), image_dir=str(dataset), max_steps=2,
                       cancel=lambda: pid_file.exists() or time.monotonic() - start > 10)
    assert result["reason"] == "cancelled"
    assert result["ok"] is False
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_brush_cannot_reuse_prior_success(brush_dataset, brush_process, monkeypatch):
    model, session, dataset = brush_dataset
    good = run_brush(str(model), str(session), image_dir=str(dataset), max_steps=2)
    assert good["ok"] is True
    previous = Path(good["splat_path"]).read_bytes()
    monkeypatch.setenv("BRUSH_TEST_MODE", "missing")
    failed = run_brush(str(model), str(session), image_dir=str(dataset), max_steps=2)
    assert failed["ok"] is False
    assert Path(good["splat_path"]).read_bytes() == previous
