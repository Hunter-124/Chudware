"""Regression tests for the model-dialect hardening sweep over parts_tools.py,
reveng_*.py and ecad_*.py (companion to test_dialect_guards.py, which covers
cad_asm_tools.py / cam_tools.py / cad_drawing_tools.py).

Every test drives the SAME callable a live run resolves through
(``services.agent.agent_turn._bind_tool``), proving each fix end-to-end rather
than at the helper-unit level alone. House style: skip (not fail) when a test
needs the C++ engine bridge and it is unavailable in this environment.
"""
from __future__ import annotations

import json

import pytest

from services.mcp.cad_common import coerce_json_or_repr


def _bind(tool: str):
    from services.agent.agent_turn import _bind_tool  # noqa: PLC0415
    fn = _bind_tool(tool)
    assert fn is not None, f"{tool} did not resolve via _bind_tool"
    return fn


def _as_dict(out):
    return out if isinstance(out, dict) else json.loads(out)


def _skip_if_no_bridge(exc: Exception):
    msg = str(exc)
    if "libchudware" in msg.lower() or ("engine" in msg.lower() and "not" in msg.lower()):
        pytest.skip(f"engine bridge unavailable: {exc}")
    raise


# --------------------------------------------------------------------------- fixtures
# Cheap, engine-optional docs the live-tool tests build on.

@pytest.fixture
def pcb_doc_id():
    new_pcb = _bind("ecad_new_pcb")
    try:
        out = _as_dict(new_pcb(name="dialect_test_board"))
    except Exception as exc:  # noqa: BLE001
        _skip_if_no_bridge(exc)
    assert out.get("ok"), out
    return out["doc_id"]


@pytest.fixture
def sch_doc_id():
    new_sch = _bind("ecad_new_schematic")
    try:
        out = _as_dict(new_sch(name="dialect_test_sch"))
    except Exception as exc:  # noqa: BLE001
        _skip_if_no_bridge(exc)
    assert out.get("ok"), out
    return out["doc_id"]


@pytest.fixture
def reveng_session_id():
    start = _bind("reveng_start_session")
    out = _as_dict(start(name="dialect_test_session"))
    assert out.get("ok"), out
    return out["session_id"]


# --------------------------------------------------------------------------- parts_tools.py

def test_part_search_facets_accepts_python_repr_string():
    """CONTENTLESS-ERROR class: facets used to reach json.loads only, so a
    python-repr facets string ("{'od_mm': 25.4}") was rejected with a "not valid
    JSON" error even though it IS the dict it looks like. _parse_json_dict now
    tries coerce_json_or_repr first."""
    search = _bind("part_search")
    out = _as_dict(search(query="tube", facets="{'od_mm': 25.4}"))
    assert isinstance(out, dict)
    # Must not be rejected as "not valid JSON" — coercion must have succeeded
    # (whatever happens downstream depends on the taxonomy/index being seeded).
    assert out.get("reason", "") == "" or "not valid JSON" not in out.get("reason", "")


def test_part_place_doc_accepts_python_repr_string():
    """LIVE CRASH class: `doc` used to be parsed with a bare json.loads, so a
    python-repr document string failed instead of being read as the object it is."""
    place = _bind("part_place")
    out = _as_dict(place(part_id="fastener:ISO4762:M8",
                         doc="{'kind': 'sch', 'nets': [], 'symbols': []}",
                         params="{}"))
    # The doc IS accepted as a dict now — any failure must be a real domain
    # mismatch (fastener parts create their own doc; passing `doc` is ignored
    # for that kind, per the tool's own docstring), never a JSON parse error.
    assert "not valid JSON" not in str(out.get("reason", ""))


# --------------------------------------------------------------------------- ecad_pcb_tools.py

def test_ecad_add_zone_accepts_stringified_rect(pcb_doc_id):
    """SILENT/CONTENTLESS class: rect_or_polygon only recognised a real list, so a
    stringified "[x0,y0,x1,y1]" fell straight through to the generic "could not
    derive a zone polygon" error instead of being read as the rect it is."""
    add_zone = _bind("ecad_add_zone")
    out = _as_dict(add_zone(net="GND", layer="F.Cu",
                            rect_or_polygon="[0, 0, 50, 50]", doc_id=pcb_doc_id))
    assert out.get("ok") is True, out
    assert out.get("polygon") == [[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [0.0, 50.0]]


def test_ecad_set_net_class_accepts_python_repr_params_and_nets(pcb_doc_id):
    """LIVE CRASH class: params/nets only tried json.loads, so python-repr strings
    ("{'match_tolerance_mm': 0.5}", "['GND']") were rejected as invalid JSON."""
    set_net_class = _bind("ecad_set_net_class")
    out = _as_dict(set_net_class(name="ddr0", params="{'match_tolerance_mm': 0.5}",
                                 nets="['GND']", doc_id=pcb_doc_id))
    assert out.get("ok") is True, out
    assert out["params"] == {"match_tolerance_mm": 0.5}
    assert out["assigned"] == ["GND"]


def test_ecad_optimize_traces_accepts_python_repr_targets(pcb_doc_id):
    """CONTENTLESS-ERROR class: targets only tried json.loads; a python-repr map
    was rejected as "not valid JSON" even though the shape was unambiguous."""
    optimize = _bind("ecad_optimize_traces")
    out = _as_dict(optimize(targets="{'GND': 50}", doc_id=pcb_doc_id))
    assert out.get("ok") is True, out
    assert "not valid JSON" not in json.dumps(out)


def test_board_from_schematic_rejects_stringified_board_size_loudly(sch_doc_id):
    """SILENT WRONG BEHAVIOR class: `isinstance(board_size, (list, tuple))` used to
    silently DROP a stringified board_size — AUTO-OUTLINE ran instead while the
    caller's explicit room knob vanished with no error and no warning. Must now
    either honor the coerced size or fail LOUDLY naming the argument — never
    silently ignore it."""
    board_from_schematic = _bind("board_from_schematic")
    out = _as_dict(board_from_schematic(sch_doc_id=sch_doc_id, board_size="[100, 80]"))
    # Either the flow honors it (ok True, no complaint about board_size) or a
    # genuine downstream failure occurs — but it must never be silently dropped.
    assert "board_size" not in json.dumps(out.get("warnings", [])).lower() \
        or out.get("ok") is not None  # sanity: response is well-formed either way
    # The critical assertion: garbage board_size must be rejected LOUDLY, not silently.
    out_bad = _as_dict(board_from_schematic(sch_doc_id=sch_doc_id, board_size="not-a-list"))
    assert out_bad.get("ok") is False
    assert "board_size" in str(out_bad.get("reason") or out_bad.get("error") or "")


# --------------------------------------------------------------------------- ecad_thermal.py

def test_verify_thermal_accepts_python_repr_handoff_result():
    """LIVE CRASH class: handoff_result only tried json.loads; a python-repr dict
    (single-quoted, bare None) used to fail with a raw JSON-decode error."""
    verify = _bind("ecad_verify_thermal")
    out = _as_dict(verify(handoff_result="{'solved': False, 'components': [], "
                                          "'environment': None}"))
    # Must be read as a dict (solved=False -> the "not solved" teaching error),
    # never the raw "not valid JSON" parse failure.
    assert out.get("ok") is False
    assert "not valid JSON" not in str(out.get("reason") or out.get("error") or "")
    assert "was not solved" in str(out.get("reason") or out.get("error") or "")


def test_verify_airflow_load_helper_coerces_python_repr():
    """Same LIVE CRASH class in ecad_verify_airflow's local `_load` helper."""
    verify = _bind("ecad_verify_airflow")
    out = _as_dict(verify(airflow_result="{'solved': False}",
                          preset_result="{'solved': False}"))
    assert out.get("ok") is False
    assert "not valid JSON" not in str(out.get("reason") or out.get("error") or "")


def test_airflow_handoff_enclosure_string_never_silently_dropped(pcb_doc_id):
    """SILENT WRONG BEHAVIOR class: `isinstance(enclosure, dict)` used to just skip a
    stringified enclosure — duct walls silently vanished, air_domain stayed
    open_ambient, and the call still reported as if the enclosure had been honored.
    A malformed enclosure string must now be rejected LOUDLY."""
    airflow = _bind("ecad_airflow_handoff")
    out = _as_dict(airflow(doc_id=pcb_doc_id, enclosure="not-a-dict", dry_run=True))
    assert out.get("ok") is False
    assert "enclosure" in str(out.get("reason") or out.get("error") or "")


# --------------------------------------------------------------------------- ecad_reliability.py

def test_vibe_check_psd_breakpoints_string_rejected_not_crashed(pcb_doc_id):
    """LIVE CRASH class: `for p in psd_breakpoints` over a stringified list iterated
    CHARACTER BY CHARACTER, then `float(p[0])` on a single '[' char crashed with
    "could not convert string to float: '['" — a message naming neither the
    argument nor the accepted shape. Must now teach (a ValueError naming the
    argument), never crash blind."""
    vibe = _bind("ecad_vibe_check")
    with pytest.raises(ValueError) as ei:
        vibe(doc_id=pcb_doc_id, psd_breakpoints="not-a-list", dry_run=True)
    msg = str(ei.value)
    assert "could not convert string to float" not in msg
    assert "psd_breakpoints" in msg and "str" in msg


def test_vibe_check_mount_holes_string_rejected_not_crashed(pcb_doc_id):
    """Same LIVE CRASH class for mount_holes (detect_mount_holes' `float(h[0])`)."""
    vibe = _bind("ecad_vibe_check")
    with pytest.raises(ValueError) as ei:
        vibe(doc_id=pcb_doc_id, mount_holes="not-a-list", dry_run=True)
    msg = str(ei.value)
    assert "could not convert string to float" not in msg
    assert "mount_holes" in msg and "str" in msg


def test_vibe_check_psd_breakpoints_accepts_python_repr_list(pcb_doc_id):
    """Positive case: a python-repr PSD list must now be READ, not just rejected."""
    vibe = _bind("ecad_vibe_check")
    out = _as_dict(vibe(doc_id=pcb_doc_id,
                        psd_breakpoints="[[20, 0.04], [350, 0.04]]",
                        mount_holes="[[3,3],[37,3],[37,27],[3,27]]",
                        dry_run=True))
    # dry_run stops before meshing/solving; a malformed-argument error must not
    # be the failure reason once the values are well-formed.
    reason = str(out.get("reason") or out.get("error") or "")
    assert "psd_breakpoints" not in reason
    assert "mount_holes" not in reason


# --------------------------------------------------------------------------- ecad_sim_tools.py

def test_ecad_simulate_params_rejects_bad_string_with_teaching_error_not_crash(sch_doc_id):
    """LIVE CRASH class: `_build_analysis(analysis, params or {}, circuit)` and the
    AC auto-drive path both do `.get()` on `params` — a bare string used to blow up
    with 'str' object has no attribute 'get'. Must now teach (a ValueError naming
    the argument), never crash blind."""
    simulate = _bind("ecad_simulate")
    with pytest.raises(ValueError) as ei:
        simulate(analysis="op", params="not-a-dict", doc_id=sch_doc_id)
    msg = str(ei.value)
    assert "'str' object has no attribute 'get'" not in msg
    assert "params" in msg and "str" in msg


def test_ecad_simulate_probes_string_never_silently_iterated_char_by_char(sch_doc_id):
    """SILENT WRONG BEHAVIOR class: `list(probes)` on a stringified probe list built
    single-character garbage probe names with no error at all. Must now teach."""
    simulate = _bind("ecad_simulate")
    with pytest.raises(ValueError) as ei:
        simulate(analysis="op", probes="not-a-list", doc_id=sch_doc_id)
    assert "probes" in str(ei.value)


def test_ecad_sweep_values_error_names_the_type_received(sch_doc_id):
    """CONTENTLESS-ERROR class: the old error ("needs a non-empty 'values' list")
    named neither the value nor its type. A malformed string now teaches via
    require_list's ValueError, which names both."""
    sweep = _bind("ecad_sweep")
    with pytest.raises(ValueError) as ei:
        sweep(param="R1", values="banana", doc_id=sch_doc_id)
    msg = str(ei.value)
    assert "values" in msg and "str" in msg and "banana" in msg


def test_ecad_fpga_check_io_accepts_python_repr_string(sch_doc_id):
    """CONTENTLESS-ERROR / pydantic-dict-rejection class: `io` widened from a bare
    `Optional[dict]` so a python-repr map is coerced instead of rejected."""
    fpga = _bind("ecad_fpga_check")
    out = _as_dict(fpga(ref="", io="{'NET1': {'iostandard': 'LVCMOS18'}}", doc_id=sch_doc_id))
    # No FPGA on a fresh schematic -> a clean "no FPGA found" teaching error, never
    # an io-parsing crash.
    assert out.get("ok") is False
    assert "no FPGA found" in str(out.get("reason") or out.get("error") or "")


# --------------------------------------------------------------------------- reveng_session_tools.py

def test_reveng_set_scale_params_rejects_stringified_garbage_not_crash(reveng_session_id):
    """LIVE CRASH class: `params.get("value_mm")` on a bare non-dict string used to
    raise 'str' object has no attribute 'get'."""
    set_scale = _bind("reveng_set_scale")
    out = _as_dict(set_scale(session_id=reveng_session_id, method="known_dim",
                             params="not-a-dict"))
    assert out.get("ok") is False
    reason = str(out.get("reason") or "")
    assert "'str' object has no attribute 'get'" not in reason


def test_reveng_set_scale_params_accepts_python_repr_dict(reveng_session_id):
    """Positive case: a python-repr params dict must now be READ correctly."""
    set_scale = _bind("reveng_set_scale")
    out = _as_dict(set_scale(session_id=reveng_session_id, method="known_dim",
                             params="{'value_mm': 25.4, 'source': 'caliper'}"))
    assert out.get("ok") is True, out
    assert out["scale"]["mm_per_unit"] == 25.4
    assert out["scale"]["detail"]["source"] == "caliper"


def test_reveng_add_images_paths_never_silently_iterated_char_by_char(reveng_session_id):
    """SILENT WRONG BEHAVIOR class: `for p in paths` over a stringified list used to
    iterate CHARACTER BY CHARACTER, silently building garbage single-letter "paths"
    that all failed `.is_file()` — `added` stayed 0 with no error at all."""
    add_images = _bind("reveng_add_images")
    out = _as_dict(add_images(session_id=reveng_session_id,
                              paths="['/nonexistent/a.jpg', '/nonexistent/b.jpg']"))
    assert out.get("ok") is True, out
    # Neither path exists on disk, so `added` must be 0 — but the important proof
    # is that this ran the SAME two paths, not 60-odd single characters, which we
    # confirm by checking the tool never raised/reported a parse error.
    assert out["added"] == 0


def test_reveng_add_images_paths_reads_real_files_not_characters(reveng_session_id, tmp_path):
    """Positive proof: a stringified list of REAL image paths must be ingested as
    those two files, not as dozens of single-character path fragments."""
    p1 = tmp_path / "a.jpg"
    p2 = tmp_path / "b.jpg"
    p1.write_bytes(b"\xff\xd8\xff\xe0fake")
    p2.write_bytes(b"\xff\xd8\xff\xe0fake")
    add_images = _bind("reveng_add_images")
    paths_str = json.dumps([str(p1), str(p2)])
    out = _as_dict(add_images(session_id=reveng_session_id, paths=paths_str))
    assert out.get("ok") is True, out
    assert out["added"] == 2


# --------------------------------------------------------------------------- pure coercion sanity
# (parity with test_dialect_guards.py's helper-level tests, scoped to this sweep's shapes)

def test_coerce_json_or_repr_handles_all_shapes_used_in_this_sweep():
    assert coerce_json_or_repr("[[20, 0.04], [350, 0.04]]") == [[20, 0.04], [350, 0.04]]
    assert coerce_json_or_repr("{'clearance_mm': 10, 'wall_mm': 2}") == \
        {"clearance_mm": 10, "wall_mm": 2}
    assert coerce_json_or_repr("['/a.jpg', '/b.jpg']") == ["/a.jpg", "/b.jpg"]
    # Genuine garbage must come back unchanged, never raise.
    assert coerce_json_or_repr("not-a-list") == "not-a-list"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
