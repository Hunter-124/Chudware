"""The compiled-sidecar dispatcher reaches the real simulation job worker.

A release sidecar is not a Python interpreter, so background simulation jobs re-enter it as
``<sidecar> --module services.sim.job_worker <job_id>``. These tests run that exact argv
through ``services.compiled_entry`` (and the source ``-m`` dialect beside it) against an
isolated job spool, executing a genuine ``selftest_sleep`` stage end to end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.sim import jobs as J

REPO = Path(__file__).resolve().parents[1]

SOURCE_DIALECT = ("-m", "services.sim.job_worker")
COMPILED_DIALECT = ("-m", "services.compiled_entry", "--module", "services.sim.job_worker")


def _isolated_env(spool: Path, *extra_path: Path) -> dict:
    env = dict(os.environ)
    env["CHUDWARE_SIM_JOBS_DIR"] = str(spool)
    env["CHUDWARE_ENGINE_DAEMON"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), *(str(p) for p in extra_path), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def _run(argv: tuple, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], cwd=str(REPO), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=60, check=False)


@pytest.mark.parametrize("dialect", [SOURCE_DIALECT, COMPILED_DIALECT],
                         ids=["source", "compiled"])
def test_dialect_runs_real_selftest_job(tmp_path, monkeypatch, dialect):
    spool = tmp_path / "sim_jobs"
    monkeypatch.setenv("CHUDWARE_SIM_JOBS_DIR", str(spool))
    job_id = "sim-dispatch" + ("c" if "--module" in dialect else "s")
    payload_json, _meta = J._validate_start(
        "test", "selftest_sleep", {"seconds": 0.1, "steps": 2}, None)
    directory = J.job_dir(job_id)
    directory.mkdir(parents=True)
    (directory / "payload.json").write_text(payload_json, encoding="utf-8")

    child = _run((*dialect, job_id), _isolated_env(spool))

    assert child.returncode == 0, (child.stdout, child.stderr)
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert "error" not in result, result
    assert result["result"]["ok"] is True
    assert result["result"]["steps"] == 2
    partial = json.loads((directory / "partial.json").read_text(encoding="utf-8"))
    assert partial["partial"] is True and partial["converged"] is False
    assert partial["state"] == "done"
    assert partial["steps_done"] == 2 and partial["pct"] == 100.0
    # The worker's own JSON-line progress stream, not a forwarding echo.
    lines = [json.loads(line) for line in child.stdout.splitlines() if line.startswith("{")]
    assert [line["steps_done"] for line in lines if "steps_done" in line][-1] == 2


def test_compiled_dialect_refuses_missing_job_id(tmp_path):
    child = _run(COMPILED_DIALECT, _isolated_env(tmp_path / "sim_jobs"))
    assert child.returncode == 2


@pytest.mark.parametrize("module", ["injected_probe", "services.sim.jobs", "os"])
def test_unknown_module_is_refused_without_import(tmp_path, module):
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    marker = tmp_path / "imported.marker"
    (probe_dir / "injected_probe.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8")

    child = _run(("-m", "services.compiled_entry", "--module", module, "sim-anything"),
                 _isolated_env(tmp_path / "sim_jobs", probe_dir))

    assert child.returncode == 2
    assert not marker.exists()
    assert not (tmp_path / "sim_jobs" / "sim-anything").exists()
