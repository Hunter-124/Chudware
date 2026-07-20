"""CAM audit + job-diff MCP tools (Wave 2).

``cam_audit`` is a thin wrapper over the C++ core RPC ``cam_audit``
(``aicad.cam_audit/1``, contracts/cam.md §4). ``cam_job_diff`` is MCP-only:
it diffs a saved ``.cam+`` ZIP against the live ``cam_tree`` and returns
``aicad.cam_job_diff/1`` (contracts/cam.md §6).

Registered via ``register_cam_audit_tools(mcp)`` immediately after
``register_cam_tools``.
"""
from __future__ import annotations

import json
import zipfile
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import core_bridge as cb
from .cam_tools import _CAM, _dumps, _err, _job


def _load_cam_plus(path: str) -> dict:
    """Load job.json from a .cam+ ZIP container (same layout as core cam_save)."""
    p = str(path or "").strip()
    if not p:
        raise ValueError("path is required")
    try:
        with zipfile.ZipFile(p, "r") as zf:
            names = set(zf.namelist())
            if "job.json" not in names:
                raise ValueError(f"no job.json in .cam+ archive: {p}")
            raw = zf.read("job.json").decode("utf-8")
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a .cam+ ZIP archive: {p}") from e
    except FileNotFoundError as e:
        raise ValueError(f"file not found: {p}") from e
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"bad job.json in {p}: {e}") from e
    if not isinstance(job, dict):
        raise ValueError(f"job.json root must be an object in {p}")
    return job


def _ops_from_job(job: dict) -> dict[str, dict]:
    """Index operations by id from a full Job::to_json payload."""
    out: dict[str, dict] = {}
    for su in job.get("setups") or []:
        if not isinstance(su, dict):
            continue
        for op in su.get("operations") or []:
            if isinstance(op, dict) and op.get("id"):
                out[str(op["id"])] = op
    return out


def _ops_from_tree(tree: dict) -> dict[str, dict]:
    """Index operations by id from a cam_tree compact payload."""
    out: dict[str, dict] = {}
    for su in tree.get("setups") or []:
        if not isinstance(su, dict):
            continue
        for op in su.get("operations") or []:
            if isinstance(op, dict) and op.get("id"):
                out[str(op["id"])] = op
    return out


def _tools_from_job(job: dict) -> set[int]:
    tools = job.get("tools") or {}
    arr = tools.get("tools") if isinstance(tools, dict) else tools
    nums: set[int] = set()
    for t in arr or []:
        if isinstance(t, dict) and t.get("number") is not None:
            try:
                nums.add(int(t["number"]))
            except (TypeError, ValueError):
                continue
    return nums


def _tools_from_tree(tree: dict) -> set[int]:
    nums: set[int] = set()
    for t in tree.get("tools") or []:
        if isinstance(t, dict) and t.get("number") is not None:
            try:
                nums.add(int(t["number"]))
            except (TypeError, ValueError):
                continue
    return nums


def _setups_from_job(job: dict) -> set[str]:
    ids: set[str] = set()
    for su in job.get("setups") or []:
        if isinstance(su, dict) and su.get("id"):
            ids.add(str(su["id"]))
    return ids


def _setups_from_tree(tree: dict) -> set[str]:
    ids: set[str] = set()
    for su in tree.get("setups") or []:
        if isinstance(su, dict) and su.get("id"):
            ids.add(str(su["id"]))
    return ids


def _scalar(v: Any) -> Any:
    """Normalize values for field compare (json-friendly)."""
    if isinstance(v, float):
        return round(v, 6)
    return v


def _op_compare_fields(saved: dict, live: dict) -> list[str]:
    """Field-level compare on params/heights/tool_number (contract §6).

    Compact tree ops only carry id/name/type/tool_number/suppressed — when the
    live side lacks heights/params/feed/rpm we only compare keys both sides have
    (or that the saved side exposes under those buckets).
    """
    fields: list[str] = []

    def note(name: str) -> None:
        if name not in fields:
            fields.append(name)

    s_tool = saved.get("tool_number")
    l_tool = live.get("tool_number")
    if s_tool is not None and l_tool is not None and int(s_tool) != int(l_tool):
        note("tool_number")

    # heights — dict compare when both present
    s_h = saved.get("heights") if isinstance(saved.get("heights"), dict) else None
    l_h = live.get("heights") if isinstance(live.get("heights"), dict) else None
    if s_h is not None and l_h is not None and s_h != l_h:
        note("heights")

    # params bucket (or top-level cutting params commonly edited)
    s_params = saved.get("params") if isinstance(saved.get("params"), dict) else {}
    l_params = live.get("params") if isinstance(live.get("params"), dict) else {}
    # Promote common top-level keys into the params compare set.
    for key in ("feed", "rpm", "stepover", "stepdown", "stock_to_leave",
                "plunge", "direction", "pattern", "cycle", "peck"):
        if key in saved and key not in s_params:
            s_params = dict(s_params)
            s_params[key] = saved[key]
        if key in live and key not in l_params:
            l_params = dict(l_params)
            l_params[key] = live[key]

    if s_params or l_params:
        keys = sorted(set(s_params) | set(l_params))
        # Only flag keys that exist on both sides, or that the saved job has and
        # the live tree also exposes (compact tree may omit most).
        for k in keys:
            if k not in live and k not in l_params and k not in saved and k not in s_params:
                continue
            if k not in live and k not in l_params:
                # live compact tree has no value — skip unless live has params dict
                if not l_params and k not in ("feed", "rpm", "stepover", "stepdown",
                                              "stock_to_leave", "plunge"):
                    continue
            sv = _scalar(s_params.get(k, saved.get(k)))
            lv = _scalar(l_params.get(k, live.get(k)))
            if lv is None and k not in live and k not in l_params:
                continue
            if sv != lv:
                note(k if k != "params" else "params")

        # If both have a params object and they differ as a whole, ensure "params"
        if (isinstance(saved.get("params"), dict) and isinstance(live.get("params"), dict)
                and saved["params"] != live["params"] and "params" not in fields):
            # only when no more specific keys already noted
            if not any(f in fields for f in ("feed", "rpm", "stepover", "stepdown",
                                            "stock_to_leave", "plunge")):
                note("params")

    return fields


def diff_cam_job(saved_job: dict, live_tree: dict) -> dict:
    """Build an aicad.cam_job_diff/1 envelope (saved .cam+ vs live cam_tree)."""
    saved_ops = _ops_from_job(saved_job)
    live_ops = _ops_from_tree(live_tree)
    saved_ids = set(saved_ops)
    live_ids = set(live_ops)

    ops_added = sorted(live_ids - saved_ids)
    ops_removed = sorted(saved_ids - live_ids)
    ops_changed: list[dict] = []
    for oid in sorted(saved_ids & live_ids):
        fields = _op_compare_fields(saved_ops[oid], live_ops[oid])
        if fields:
            ops_changed.append({"op_id": oid, "fields": fields})

    s_tools = _tools_from_job(saved_job)
    l_tools = _tools_from_tree(live_tree)
    tools_added = sorted(l_tools - s_tools)
    tools_removed = sorted(s_tools - l_tools)

    s_setups = _setups_from_job(saved_job)
    l_setups = _setups_from_tree(live_tree)
    setups_added = sorted(l_setups - s_setups)
    setups_removed = sorted(s_setups - l_setups)

    parts: list[str] = []
    if ops_added:
        parts.append(f"{len(ops_added)} op{'s' if len(ops_added) != 1 else ''} added")
    if ops_removed:
        parts.append(f"{len(ops_removed)} op{'s' if len(ops_removed) != 1 else ''} removed")
    if ops_changed:
        field_names: list[str] = []
        for ch in ops_changed:
            for f in ch.get("fields") or []:
                if f not in field_names:
                    field_names.append(f)
        chg = f"{len(ops_changed)} op{'s' if len(ops_changed) != 1 else ''} changed"
        if field_names:
            chg += f" ({', '.join(field_names)})"
        parts.append(chg)
    if tools_added or tools_removed:
        tbits = []
        if tools_added:
            tbits.append(f"+{len(tools_added)}")
        if tools_removed:
            tbits.append(f"-{len(tools_removed)}")
        parts.append(f"tools {', '.join(tbits)}")
    else:
        parts.append("tools unchanged")
    if setups_added:
        parts.append(f"{len(setups_added)} setup(s) added")
    if setups_removed:
        parts.append(f"{len(setups_removed)} setup(s) removed")
    if not parts:
        parts.append("no changes")
    summary = ", ".join(parts)

    return {
        "schema": "aicad.cam_job_diff/1",
        "ok": True,
        "ops_added": ops_added,
        "ops_removed": ops_removed,
        "ops_changed": ops_changed,
        "tools_added": tools_added,
        "tools_removed": tools_removed,
        "setups_added": setups_added,
        "setups_removed": setups_removed,
        "summary": summary,
    }


def register_cam_audit_tools(mcp: FastMCP) -> None:
    """Attach cam_audit + cam_job_diff to the FastMCP server."""

    @mcp.tool()
    def cam_audit(
        doc_id: str = "",
        gcode: str = "",
        dialect: str = "",
        machine: str = "",
        setup_id: str = "",
    ) -> str:
        """Deterministic G-code verification (aicad.cam_audit/1, contracts/cam.md §4).

        Thin wrapper over the C++ core ``cam_audit`` RPC. Checks (no LLM):
          1. Modal parse — G0/G1/G2/G3/G4, planes, units, work offsets, G90/G91,
             M3/M4/M5/M6, T/S/F, XYZ IJK R; unknown G/M → warning.
          2. Units — G20 (inch) is an error; jobs are mm.
          3. Envelope — position within stock expanded by machine travel; Z floor.
          4. Feeds/speeds — S/F within machine profile; chipload vs CuttingDB → warn.
          5. Tools — every T must be in the job tool library.
          6. Round-trip — parsed cut extents vs toolpath bbox (+ tool radius).
          7. Rapids below clearance — G0 under WCS top inside stock XY → warning.

        When ``gcode`` is empty the core posts the job/setup in-memory (same path
        as cam_post). ``machine`` selects a MachineDB profile (default benchtop).
        Returns ``{schema, ok, errors, warnings, stats, provenance}``;
        ``ok=false`` iff ``errors`` is non-empty.
        """
        try:
            jid = (doc_id or "").strip() or _job()
            params: dict[str, Any] = {"doc_id": jid}
            if gcode:
                params["gcode"] = gcode
            if dialect:
                params["dialect"] = dialect
            if machine:
                params["machine"] = machine
            if setup_id:
                params["setup_id"] = setup_id
            return _dumps(cb.call("cam_audit", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_job_diff(path: str) -> str:
        """Diff a saved ``.cam+`` file against the current CAM job tree
        (``aicad.cam_job_diff/1``, contracts/cam.md §6).

        Loads ``job.json`` from the ZIP container written by ``cam_save``, reads
        the live tree via ``cam_tree``, and reports ops/tools/setups
        added/removed plus field-level op changes on ``params`` / ``heights`` /
        ``tool_number``. Returns a one-line ``summary``.
        """
        try:
            saved = _load_cam_plus(path)
            tree_r = cb.call("cam_tree", {"doc_id": _job()})
            tree = tree_r.get("tree") if isinstance(tree_r.get("tree"), dict) else tree_r
            if not isinstance(tree, dict):
                tree = {}
            return _dumps(diff_cam_job(saved, tree))
        except Exception as e:  # noqa: BLE001
            return _err(e)
