from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.manufacturing.monitor.mtconnect import parse_current, parse_probe
from services.manufacturing.monitor.normalize import EndpointContext

from .codec import BOOLEAN, STRING, UINT32, DataValue, NodeId, Variant
from .machine_tools import (
    MachineToolShape,
    NodeReading,
    SUPPORTED_MACHINE_TOOL_SHAPES,
    map_machine_tools,
)

MT_FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "mfg" / "monitor" / "mtconnect"
STAMP = datetime(2026, 8, 8, 12, 0, 0, 200000, tzinfo=timezone.utc)
REMOTE = EndpointContext(
    endpoint="opc.tcp://machine.example.test:4840",
    configured_label="mill-one",
    origin="live",
    endpoint_is_physical=True,
)


def _mt_fixture(name: str) -> str:
    return (MT_FIXTURES / name).read_text(encoding="utf-8")


def _shape(path: str) -> MachineToolShape:
    return next(item for item in SUPPORTED_MACHINE_TOOL_SHAPES if item.browse_path == path)


def test_machine_tools_execution_row_has_mtconnect_canonical_parity() -> None:
    mt_context = EndpointContext(
        endpoint="https://machine.example.test:5000",
        configured_label="mill-one",
        origin="live",
        endpoint_is_physical=True,
    )
    probe = parse_probe(_mt_fixture("probe_v2.xml"), mt_context)
    mt_row = next(
        row
        for row in parse_current(
            _mt_fixture("current_running_v2.xml"), mt_context, probe
        ).observations
        if row.source_item_id == "exec"
    )
    path = "/MachineTool/Monitoring/ExecutionState"
    opc_row = map_machine_tools(
        {path: NodeReading(NodeId(2, 1101), DataValue(Variant(STRING, "ACTIVE"), 0, STAMP, STAMP))},
        REMOTE,
        device_name="mill-one",
        shapes=(_shape(path),),
    ).observations[0]

    assert (
        opc_row.canonical_kind,
        opc_row.value_kind,
        opc_row.v_num,
        opc_row.v_text,
        opc_row.quality,
        opc_row.state,
        opc_row.t_src_utc_ns,
    ) == (
        mt_row.canonical_kind,
        mt_row.value_kind,
        mt_row.v_num,
        mt_row.v_text,
        mt_row.quality,
        mt_row.state,
        mt_row.t_src_utc_ns,
    )
    assert opc_row.raw["value"]["value"] == "ACTIVE"
    assert opc_row.schema.protocol == "opcua"


def test_bad_status_missing_timestamp_and_absent_node_are_unknown_without_defaults() -> None:
    execution = "/MachineTool/Monitoring/ExecutionState"
    spindle = "/MachineTool/Monitoring/Spindle/ActualSpeed"
    parts = "/MachineTool/Monitoring/PartCount"
    rows = map_machine_tools(
        {
            execution: DataValue(Variant(STRING, "ACTIVE"), 0x80340000, STAMP, STAMP),
            spindle: DataValue(Variant(UINT32, 9000), 0, None, STAMP),
            parts: None,
        },
        REMOTE,
        device_name="mill-one",
        shapes=(_shape(execution), _shape(spindle), _shape(parts)),
    ).observations
    bad, missing_time, absent = rows

    assert bad.state == "unknown"
    assert bad.quality == "bad"
    assert bad.why == "node is absent from the server address space (BadNodeIdUnknown)"
    assert bad.v_text is None
    assert bad.raw["value"]["value"] == "ACTIVE"

    assert missing_time.state == "unknown"
    assert missing_time.quality == "unavailable"
    assert missing_time.why == "missing source timestamp"
    assert missing_time.v_num is None
    assert missing_time.t_server_utc_ns is not None
    assert missing_time.raw["value"]["value"] == 9000

    assert absent.state == "unknown"
    assert absent.quality == "unavailable"
    assert absent.why == "typed profile node is absent"
    assert absent.v_num is absent.v_text is absent.v_json is None


def test_array_value_is_typed_json_and_loopback_forces_simulated_source() -> None:
    path = "/MachineTool/Monitoring/PartCount"
    loopback = EndpointContext(
        endpoint="opc.tcp://127.0.0.1:4840/machine",
        configured_label="fixture",
        origin="live",
        endpoint_is_physical=True,
    )
    row = map_machine_tools(
        {path: DataValue(Variant(UINT32, [1, 2, 3], True), 0, STAMP, STAMP)},
        loopback,
        device_name="fixture",
        shapes=(_shape(path),),
    ).observations[0]

    assert row.v_json == [1, 2, 3]
    assert row.v_num is row.v_text is None
    assert row.src == "simulated"
    assert row.evidence_eligible is False


def test_supported_emergency_stop_shape_maps_to_condition_row() -> None:
    path = "/MachineTool/Monitoring/EmergencyStop"
    row = map_machine_tools(
        {path: DataValue(Variant(BOOLEAN, True), 0, STAMP, STAMP)},
        REMOTE,
        device_name="mill-one",
        shapes=(_shape(path),),
    ).conditions[0]

    assert row.severity == "fault"
    assert row.transition == "raised"
    assert row.state == "unknown"
    assert row.quality == "bad"
    assert row.why == "source reported active emergency-stop condition"
    assert row.raw["value"]["value"] is True
