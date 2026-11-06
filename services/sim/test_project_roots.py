from __future__ import annotations

import os
import sys

import pytest

from services.agent import request_context
from services.sim import jobs


# Run the production worker and its stage thread in a real child process. Only
# the otherwise slow solver stage is replaced with a root-observing stage.
_WORKER_PROBE = r'''
import sys
import time
from services.agent.request_context import root_value
from services.mcp.cad_common import resolve_agent_path
from services.mcp.cad_render_tools import _render_save_target
from services.sim import job_worker, jobs

def probe(payload, emit):
    time.sleep(0.2)
    return {
        "ok": True,
        "project": root_value("CHUDWARE_PROJECT_DIR"),
        "save": root_value("CHUDWARE_PROJECT_SAVE_DIR"),
        "fs": root_value("CHUDWARE_FS_ROOT"),
        "output": resolve_agent_path("parts/child.step+", "output path"),
        "render": _render_save_target("figures/child.png"),
    }

jobs.STAGES["selftest_sleep"] = probe
raise SystemExit(job_worker.main(sys.argv))
'''


@pytest.mark.parametrize("ambient_set", [False, True])
def test_desktop_durable_jobs_keep_initiating_turn_roots_after_completion(
    tmp_path, monkeypatch, ambient_set
):
    registry = tmp_path / "registry"
    ambient = tmp_path / "ambient"
    projects = [tmp_path / "first" / "selected", tmp_path / "second" / "selected"]
    for directory in [ambient, *projects]:
        directory.mkdir(parents=True)
    monkeypatch.setenv("CHUDWARE_SIM_JOBS_DIR", str(registry))
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    monkeypatch.delenv("CHUDWARE_RENDER_SAVE_ANYWHERE", raising=False)
    for name in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR", "CHUDWARE_FS_ROOT"):
        if ambient_set:
            monkeypatch.setenv(name, str(ambient))
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(jobs, "_module_command", lambda _: [sys.executable, "-c", _WORKER_PROBE])

    children = []
    try:
        for project in projects:
            save = project / "saved"
            with request_context.bind_roots(str(project), str(save)):
                started = jobs.start("test", "selftest_sleep", {"seconds": 1, "steps": 1})
                children.append((project, save, started["job_id"], jobs._LOCAL_PROCS[started["job_id"]]))
        # Both turns have unbound their contexts before either result is collected.
        for name in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR", "CHUDWARE_FS_ROOT"):
            assert os.environ.get(name) == (str(ambient) if ambient_set else None)
        for project, save, job_id, process in children:
            assert process.wait(timeout=30) == 0, (registry / job_id / "worker.log").read_text()
            status = jobs.status(job_id)
            assert status["state"] == jobs.DONE, status
            assert jobs.raw_result(job_id) == {
                "ok": True,
                "project": str(project), "save": str(save), "fs": str(project),
                "output": str(project / "parts" / "child.step+"),
                "render": str(project / "figures" / "child.png"),
            }
    finally:
        for _, _, _, process in children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
