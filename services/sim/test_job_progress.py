"""Progress a long mesh job reports: monotonic fill labelling, CPU-aware stall, MCP remedies."""
from __future__ import annotations

import json
import os
import time

import pytest

from services.sim import job_worker
from services.sim import jobs as J
from services.sim import mesh_remedy


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_SIM_JOBS_DIR", str(tmp_path))
    J._LOCAL_PROCS.clear()
    return tmp_path


def _running_job(job_id: str, *, state: str = J.RUNNING) -> None:
    J.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    now = time.time()
    with J._db() as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,proc_key,created_at,"
            "started_at,cancel_requested,meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, "fea_static", "core_rpc", state, os.getpid(),
             J._proc_key(os.getpid()), now, now, 0, "{}"))


def _probe(sub_phase: str, elements: int) -> dict:
    return {"phase": "mesh_background_probe", "elements": elements,
            "probe": {"h_mm": 6.0, "cells_done": 0, "cells_total": 0,
                      "elements_so_far": elements, "sub_phase": sub_phase}}


def test_repeated_fills_are_numbered_not_reset(registry, capsys):
    """sim-46ea4662cf7e: passes 7/7 -> 1/7 and 137,180 -> 58,446 were separate fills."""
    job_id = "sim-fills"
    _running_job(job_id)
    progress = job_worker._Progress(J.job_dir(job_id), job_id, clock=lambda: 0.0,
                                    cpu_clock=lambda: 0.0)
    progress.emit({"phase": "mesh_background_probe"})
    progress.emit(_probe("projection_pass 7/7", 137180))
    progress.emit(_probe("complete", 137180))
    first = J.partial(job_id)
    assert first["fill"] == {"index": 1, "completed": 0, "completed_elements": []}
    assert "mesh fill 1 (0 completed)" in first["summary"]
    assert "fill done, finishing" in first["summary"]

    progress.emit({"phase": "mesh_background_probe_complete", "elements": 137180})
    assert J.partial(job_id)["fill"] == {"index": 1, "completed": 1,
                                         "completed_elements": [137180]}
    # The native event repeats the finished fill's probe until the next fill reports.
    progress.emit({"phase": "mesh_background_probe", **{k: v for k, v in
                   _probe("complete", 137180).items() if k != "phase"}})
    between = J.partial(job_id)
    assert between["fill"]["index"] == 2
    assert "mesh_background_probe: starting" in between["summary"]
    assert "elements in this fill" not in between["summary"]

    progress.emit(_probe("projection_pass 1/7", 58446))
    second = J.partial(job_id)
    assert second["fill"] == {"index": 2, "completed": 1, "completed_elements": [137180]}
    assert "mesh fill 2 (1 completed)" in second["summary"]
    assert "projection_pass 1/7" in second["summary"]
    assert "58446 elements in this fill" in second["summary"]
    assert J.status(job_id)["progress"]["fill"]["index"] == 2
    capsys.readouterr()


def test_solid_label_rides_on_native_events(registry, capsys):
    job_id = "sim-solids"
    _running_job(job_id)
    progress = job_worker._Progress(J.job_dir(job_id), job_id, clock=lambda: 0.0,
                                    cpu_clock=lambda: 0.0)
    progress.emit({**_probe("leb_wave 1/1", 100), "body": {"index": 3, "total": 12}})
    assert J.partial(job_id)["summary"].startswith("solid 3/12; mesh fill 1")
    capsys.readouterr()


def test_busy_native_phase_is_not_stalled_but_idle_one_is(registry, monkeypatch, capsys):
    """Counters can sit still for 150+ s inside one native sub-phase that is computing."""
    job_id = "sim-busy"
    _running_job(job_id)
    now = [0.0]
    cpu = [0.0]
    monkeypatch.setattr(J, "_clock", lambda: now[0])
    progress = job_worker._Progress(J.job_dir(job_id), job_id, clock=lambda: now[0],
                                    cpu_clock=lambda: cpu[0])
    progress.emit(_probe("exterior_edge_projection", 58354))
    for instant in (30.0, 60.0, 90.0, 120.0, 150.0, 180.0):
        now[0] = instant
        cpu[0] += 25.0  # the native thread is computing
        progress.heartbeat()
    busy = J.status(job_id)["progress"]
    assert busy["last_advance_s"] == 180.0
    assert busy["stalled"] is False
    assert busy["last_cpu_active_s"] == 0.0
    assert "long native phase" in busy["stall_note"]

    for instant in (210.0, 240.0, 270.0, 300.0, 330.0):
        now[0] = instant  # no CPU, no counters: blocked
        progress.heartbeat()
    idle = J.status(job_id)["progress"]
    assert idle["last_cpu_active_s"] == 150.0
    assert idle["stalled"] is True
    assert "stall_note" not in idle
    capsys.readouterr()


def test_worker_without_cpu_field_keeps_counter_rule(registry, monkeypatch):
    job_id = "sim-legacy"
    _running_job(job_id)
    (J.job_dir(job_id) / "partial.json").write_text(json.dumps(
        {"phase": "mesh_background_probe", "partial": True,
         "_last_advance_monotonic": 0.0, "last_advance_s": 0.0}), encoding="utf-8")
    monkeypatch.setattr(J, "_clock", lambda: 121.0)
    assert J.status(job_id)["progress"]["stalled"] is True


VOLUME_GUARD = (
    "polymesh mesher failed: solid 4: geometry fill-stage guard failed at h=10 mm "
    "(requested -h 10 mm): mesh/BRep volume relative error 0.04 exceeds hard limit 0.02; "
    "the lattice does not resolve the solid. Reducing -h a little does NOT fix this -- "
    "the error is set by which features the lattice straddles. Reduce -h to <= 5 mm, or "
    "raise --max-elems/--max-dof to afford it. (Fill census: 10 cells.) | note")
SKIN_GUARD = (
    "geometry fill-stage guard failed at h=8 mm (requested -h 8 mm): the boundary is not "
    "a closed surface. This is a mesher defect, not a resolution one, and refining does "
    "not clear it. Retry with the default --mesher hybrid, which is watertight on every "
    "part measured | note")
FEATURE = ("feature unresolved at h=4.45857 mm: CAD face 6 ... reduce -h to <= 2.68 mm "
           "or raise --max-elems/--max-dof; polymesh needs an element size of 1.2 mm to "
           "resolve this feature, below this study's element-size band floor of 2 mm — "
           "raise fineness or set min_size_mm to mesh it")
CEILING = ("mesh element ceiling 589824 exceeded: predicted 700000 elements at h=1 mm; "
           "increase -h or raise --max-elems")


@pytest.mark.parametrize("native", [VOLUME_GUARD, SKIN_GUARD, FEATURE, CEILING])
def test_cli_remedies_become_mcp_parameters(native):
    text = mesh_remedy.to_mcp(native)
    for flag in ("--max-elems", "--max-dof", "--mesher", "requested -h", "Reduce -h",
                 "reduce -h", "increase -h", "Reducing -h"):
        assert flag not in text, (flag, text)
    assert "mesh_size_mm" in text and "millimetres" in text
    assert "not tool arguments" in text
    assert mesh_remedy.to_mcp(text) == text  # idempotent


def test_remedy_keeps_numbers_and_names_the_retry():
    text = mesh_remedy.to_mcp(VOLUME_GUARD)
    assert "(requested element size 10 mm)" in text
    assert "Set mesh_size_mm <= 5. (Fill census" in text
    assert "Reducing mesh_size_mm a little" in text
    assert text.endswith("Suggested retry: mesh_size_mm=5.")
    skin = mesh_remedy.to_mcp(SKIN_GUARD)
    assert "graded-tet mesher (there is no mesher argument)" in skin
    assert "Suggested retry" not in skin
    assert "raise mesh_fineness (or lower mesh_size_mm) or set min_size_mm" in (
        mesh_remedy.to_mcp(FEATURE))
    assert "increase mesh_size_mm" in mesh_remedy.to_mcp(CEILING)


def test_unrelated_text_is_untouched():
    for text in ("solver diverged", "h=4 mm is fine", ""):
        assert mesh_remedy.to_mcp(text) == text
    assert mesh_remedy.apply({"a": [VOLUME_GUARD], "b": 1})["a"][0].startswith(
        "polymesh mesher failed")


def test_status_rewrites_a_failed_jobs_error(registry):
    job_id = "sim-guard"
    _running_job(job_id, state=J.FAILED)
    J._set(job_id, error=VOLUME_GUARD, done_at=time.time())
    error = J.status(job_id)["error"]
    assert "--max-elems" not in error and "Set mesh_size_mm <= 5" in error
