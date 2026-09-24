from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
import re

import pytest

from services.manufacturing.monitor.mtconnect import (
    MalformedDocumentRefusal,
    UnsupportedVersionRefusal,
    parse_current,
    parse_probe,
)
from services.manufacturing.monitor.normalize import EndpointContext

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "mfg" / "monitor" / "mtconnect"
CONTEXT = EndpointContext(
    endpoint="https://machine.example.test:5000",
    configured_label="mill-one",
    origin="live",
    endpoint_is_physical=True,
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_probe_maps_data_items_and_keeps_source_schema_and_raw_element() -> None:
    probe = parse_probe(fixture("probe_v2.xml"), CONTEXT)

    assert probe.document_kind == "probe"
    assert probe.schema.protocol_version == "2.0"
    assert probe.schema.profile == "MTConnect Devices 2.0"
    assert "urn:example:vendor" in probe.schema.extension_namespaces
    assert len(probe.devices) == 1
    by_source_id = probe.data_item_map()
    position = by_source_id["x_pos"]
    assert position.canonical_kind == "axis_position"
    assert position.value_kind == "sample"
    assert position.unit == "mm"
    assert position.sub_type == "ACTUAL"
    assert "channel=\"primary\"" in position.raw_element


def test_current_maps_rows_and_retains_raw_source_element() -> None:
    probe = parse_probe(fixture("probe_v2.xml"), CONTEXT)
    current = parse_current(fixture("current_running_v2.xml"), CONTEXT, probe)

    rows = {row.source_item_id: row for row in current.observations}
    position = rows["x_pos"]
    assert position.canonical_kind == "axis_position"
    assert position.v_num == 12.5
    assert position.v_text is None
    assert position.t_src_utc_ns == 1_786_190_400_300_000_000
    assert position.src_seq == 3
    assert position.seq is None
    assert position.quality == "good"
    assert position.state == "ok"
    assert position.src == "probed"
    assert position.evidence_eligible is True
    assert position.raw["attributes"]["sequence"] == "3"
    assert "confidence=\"high\"" in position.raw_element


def test_prefixed_mtconnect_namespace_is_accepted() -> None:
    probe = parse_probe(fixture("probe_v2.xml"), CONTEXT)
    current = parse_current(fixture("prefixed_current_v2.xml"), CONTEXT, probe)

    assert current.schema.namespace_uri == "urn:mtconnect.org:MTConnectStreams:2.0"
    assert current.observations[0].source_item_id == "exec"
    assert current.observations[0].v_text == "ACTIVE"


def test_unavailable_is_unknown_and_never_exposes_last_good_as_current() -> None:
    probe = parse_probe(fixture("probe_v2.xml"), CONTEXT)
    row = parse_current(fixture("unavailable_v2.xml"), CONTEXT, probe).observations[0]

    assert row.quality == "unavailable"
    assert row.quality_src == "UNAVAILABLE"
    assert row.state == "unknown"
    assert row.why == "source reported UNAVAILABLE"
    assert row.v_num is row.v_text is row.v_json is None
    assert row.last_ok is None
    assert row.evidence_eligible is False


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "1e309", "garbage", ""])
def test_invalid_numeric_sample_cannot_be_good_evidence(text: str) -> None:
    probe = parse_probe(fixture("probe_v2.xml"), CONTEXT)
    current_xml = fixture("current_running_v2.xml").replace(
        ">12.5</Position>", f">{text}</Position>"
    )
    rows = {
        row.source_item_id: row for row in parse_current(current_xml, CONTEXT, probe).observations
    }
    row = rows["x_pos"]

    assert row.quality == "unavailable"
    assert row.quality_src == "INVALID_NUMERIC_VALUE"
    assert row.state == "unknown"
    assert row.why == "numeric sample is non-finite or unparseable"
    assert row.v_num is row.v_text is row.v_json is None
    assert row.evidence_eligible is False
    assert row.raw["text"] == text
    assert rows["exec"].quality == "good"
    assert rows["exec"].v_text == "ACTIVE"


def test_numeric_unit_conversion_overflow_is_unavailable() -> None:
    probe = parse_probe(fixture("probe_v2.xml"), CONTEXT)
    position = probe.data_item_map()["x_pos"]
    inch_position = replace(position, unit_src="INCH")
    current_xml = fixture("current_running_v2.xml").replace(
        ">12.5</Position>", ">1e308</Position>"
    )
    row = next(
        row for row in parse_current(current_xml, CONTEXT, {"x_pos": inch_position}).observations
        if row.source_item_id == "x_pos"
    )

    assert row.quality == "unavailable"
    assert row.quality_src == "INVALID_NUMERIC_VALUE"
    assert row.state == "unknown"
    assert row.v_num is row.v_text is row.v_json is None
    assert row.evidence_eligible is False


def test_condition_has_falsify_and_retain_as_last_ok_policy() -> None:
    probe = parse_probe(fixture("probe_v2.xml"), CONTEXT)
    condition = parse_current(fixture("condition_fault_v2.xml"), CONTEXT, probe).conditions[0]

    assert condition.severity == "fault"
    assert condition.quality == "bad"
    assert condition.state == "unknown"
    assert condition.native_code == "E42"
    assert condition.native_severity == "CRITICAL"
    assert condition.qualifier == "HIGH"
    assert condition.transition == "raised"
    assert condition.epoch_policy == "falsify"
    assert condition.retain_as_last_ok is True
    assert "Fixture spindle fault" in condition.raw_element


def test_malformed_document_is_an_explicit_typed_refusal() -> None:
    with pytest.raises(MalformedDocumentRefusal) as caught:
        parse_current(fixture("malformed.xml"), CONTEXT)

    assert caught.value.code == "malformed_document"
    assert "XML parse error" in caught.value.reason


def test_version_skew_is_an_explicit_typed_refusal() -> None:
    with pytest.raises(UnsupportedVersionRefusal) as caught:
        parse_current(fixture("version_skew.xml"), CONTEXT)

    assert caught.value.code == "unsupported_version"
    assert "9.9" in caught.value.reason


def test_monitor_modules_have_no_command_or_network_client_entry_point() -> None:
    package = Path(__file__).parent
    forbidden_definition = re.compile(
        r"(?:^|_)(?:write|call|invoke|command|exec|set|start|stop|reset|ack|acknowledge|confirm|"
        r"override|jog|home|zero|unlock|resume)(?:_|$)",
        re.IGNORECASE,
    )
    forbidden_imports = {"socket", "http.client", "urllib.request", "requests", "subprocess"}

    for module_name in ("__init__.py", "normalize.py", "mtconnect.py", "replay.py"):
        tree = ast.parse((package / module_name).read_text(encoding="utf-8"), filename=module_name)
        definitions = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert not [name for name in definitions if forbidden_definition.search(name)], module_name

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert imported.isdisjoint(forbidden_imports), module_name
