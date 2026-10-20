"""GUI DFAM endpoint over the same topology report used by the model tools."""
from __future__ import annotations

import json
from pathlib import Path

from services.sim import topology as topo


def _topology_dfam(params: dict, emit) -> dict:
    del emit
    try:
        if not isinstance(params, dict):
            raise topo.TopologyInputError("invalid_input", "params must be an object")
        run_dir = params.get("run_dir")
        if not isinstance(run_dir, str) or not Path(run_dir).is_absolute():
            raise topo.TopologyInputError("invalid_input", "run_dir must be an absolute path")
        directory = Path(run_dir).resolve()
        result_path = directory / "result.json"
        if not result_path.is_file():
            raise topo.TopologyInputError("invalid_result", "run_dir has no result.json")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise topo.TopologyInputError("invalid_result", f"Cannot read result.json: {exc}") from None
        if not isinstance(result, dict) or result.get("method") not in topo.METHODS \
                or result.get("physics") not in topo.PHYSICS \
                or not isinstance(result.get("topology"), dict) \
                or not isinstance(result.get("manufacturing"), dict) \
                or not isinstance(result.get("artifacts"), dict):
            raise topo.TopologyInputError("invalid_result", "result.json is not a topology result")
        process = topo.print_process(params["print_process"]) if "print_process" in params else None
        surface = topo.surface_artifact(result["artifacts"], intact=False)
        if surface is None:
            raise topo.TopologyInputError("artifact_mismatch", "No surface artifact was reported")
        key, entry = surface
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise topo.TopologyInputError("artifact_mismatch", f"Artifact {key} has no path")
        path = Path(path)
        if not path.is_absolute():
            path = directory / path
        digest = entry.get("sha256")
        if not topo.contained(path, directory) or not path.is_file() \
                or not isinstance(digest, str) or not digest \
                or topo.sha256_file(path) != digest.lower() \
                or entry.get("missing") or entry.get("stale"):
            raise topo.TopologyInputError(
                "artifact_mismatch", f"Artifact {key} is missing or does not match its sha256")
        artifacts = {key: {**entry, "path": str(path.resolve())}}
        manufacturing = topo.dfam_report(result, artifacts, result["topology"], process)
        return {"ok": True, "dfam": manufacturing["dfam"], "print_ready": False,
                "print_ready_reason": manufacturing["print_ready_reason"]}
    except topo.TopologyInputError as exc:
        return {"ok": False, "code": exc.code, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — worker failures are structured payloads
        return {"ok": False, "code": "dfam_failed", "error": f"{type(exc).__name__}: {exc}"}


WORKER_METHODS = {"topology_dfam": _topology_dfam}
