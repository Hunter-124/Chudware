"""Firmware worker endpoints (T-FIN5) — the GUI-facing mirror of the fw_* MCP tools.

Registered into the JSON-RPC worker (``engine_oracle/worker.py``) so the desktop Firmware
workspace drives the SAME single implementation as the MCP tools: every handler here is a
thin ``(params, emit)`` adapter over ``engine.firmware.mcp_tools`` (which is already
framework-free, dict-in → dict-out; see ``contracts/firmware_rpc.md``).

Additions beyond the MCP mirror:

- ``fw_status``  — the T-FIN4 provisioning doctor (``engine.firmware.provision.fw_status``),
  so the GUI can gate Build/Emulate/Flash buttons honestly and surface install guidance.
- ``fw_projects`` — lists on-disk firmware projects (``<firmware_root>/<id>/project.json``)
  with the absolute ``root_dir`` so the GUI can show sources / read files locally.

All handlers follow the firmware contract's graceful posture: expected failures come back as
result payloads (``{"code", "message", "detail"}`` envelope or ``success=False``), never as
raised exceptions.
"""

from __future__ import annotations

from typing import Callable

from engine.firmware import mcp_tools as _fw
from engine.firmware import project as fw_project
from engine.firmware.provision import fw_status as _fw_status


def _wrap(fn: Callable[[dict], dict]) -> Callable[[dict, Callable], dict]:
    """Adapt a dict-in → dict-out mcp_tools handler to the worker's (params, emit) shape."""

    def handler(params: dict, emit) -> dict:  # noqa: ANN001 - worker emit callback
        del emit  # firmware handlers are synchronous; no streamed progress today
        return fn(params or {})

    return handler


def _fw_status_rpc(params: dict, emit) -> dict:  # noqa: ANN001
    del params, emit
    return _fw_status()


def _fw_projects(params: dict, emit) -> dict:  # noqa: ANN001
    """``{}`` → ``{projects: [{...project, root_dir}]}`` — newest first.

    Scans ``firmware_root()`` for ``<id>/project.json`` sidecars. Unreadable sidecars are
    skipped (never fatal): a half-written project must not take down the whole list.
    """
    del params, emit
    root = fw_project.firmware_root()
    projects: list[dict] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            sidecar = child / "project.json"
            if not sidecar.is_file():
                continue
            try:
                p = fw_project.load(child.name)
            except Exception:  # noqa: BLE001 - skip corrupt sidecars, keep listing
                continue
            d = p.to_dict()
            d["root_dir"] = str(p.root_dir)
            projects.append(d)
    projects.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return {"projects": projects}


WORKER_METHODS: dict[str, Callable] = {
    "fw_status": _fw_status_rpc,
    "fw_projects": _fw_projects,
    "fw_new_project": _wrap(_fw.fw_new_project),
    "fw_build": _wrap(_fw.fw_build),
    "fw_sources": _wrap(_fw.fw_sources),
    "fw_sim_run": _wrap(_fw.fw_sim_run),
    "fw_verify_board": _wrap(_fw.fw_verify_board),
    "fw_flash": _wrap(_fw.fw_flash),
    "fw_debug_start": _wrap(_fw.fw_debug_start),
    "fw_debug_halt": _wrap(_fw.fw_debug_halt),
    "fw_debug_read_mem": _wrap(_fw.fw_debug_read_mem),
}
