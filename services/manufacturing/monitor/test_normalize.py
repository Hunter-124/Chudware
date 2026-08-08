from __future__ import annotations

from pathlib import Path

from services.manufacturing.monitor.mtconnect import parse_current, parse_probe, parse_sample
from services.manufacturing.monitor.normalize import (
    EndpointContext,
    SequenceTracker,
    condition_snapshot,
    conditions_unknown_on_gap,
    derive_src,
    observations_unknown_on_gap,
    value_snapshot,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "mfg" / "monitor" / "mtconnect"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_source_truth_is_derived_from_endpoint_physicality() -> None:
    assert (
        derive_src(
            endpoint="https://machine.example.test:5000",
            origin="live",
            endpoint_is_physical=True,
        )
        == "probed"
    )
    assert (
        derive_src(endpoint="http://127.0.0.1:5000", origin="live", endpoint_is_physical=True)
        == "simulated"
    )
    assert (
        derive_src(
            endpoint="https://machine.example.test:5000",
            origin="replay_fixture",
            endpoint_is_physical=True,
        )
        == "simulated"
    )
    assert (
        derive_src(
            endpoint="https://machine.example.test:5000",
            origin="live",
            endpoint_is_physical=True,
            manifest_virtual=True,
        )
        == "simulated"
    )


def test_buffer_roll_forces_unknown_and_only_retains_last_good_for_display() -> None:
    context = EndpointContext(
        endpoint="https://machine.example.test:5000",
        configured_label="mill-one",
        origin="live",
        endpoint_is_physical=True,
    )
    probe = parse_probe(fixture("probe_v2.xml"), context)
    current = parse_current(fixture("current_running_v2.xml"), context, probe)
    rolled = parse_sample(fixture("gap_buffer_roll_v2.xml"), context, probe)
    tracker = SequenceTracker()

    assert tracker.check(context.source_id, current.header) is None
    gap = tracker.check(context.source_id, rolled.header)
    assert gap is not None
    assert gap.reason == "buffer_roll"
    assert gap.invalidation_class == "falsified"

    prior_position = next(row for row in current.observations if row.source_item_id == "x_pos")
    normalized = observations_unknown_on_gap(
        rolled.observations,
        gap,
        {prior_position.item_key: value_snapshot(prior_position)},
    )[0]
    assert normalized.state == "unknown"
    assert normalized.quality == "unavailable"
    assert normalized.quality_src == "SEQUENCE_GAP"
    assert normalized.gap is True
    assert normalized.evidence_eligible is False
    assert normalized.v_num is normalized.v_text is normalized.v_json is None
    assert normalized.last_ok["v_num"] == 12.5
    assert normalized.last_ok["src_seq"] == 3


def test_condition_gap_falsifies_current_value_but_retains_normal_as_last_ok() -> None:
    context = EndpointContext(
        endpoint="https://machine.example.test:5000",
        configured_label="mill-one",
        origin="live",
        endpoint_is_physical=True,
    )
    probe = parse_probe(fixture("probe_v2.xml"), context)
    current = parse_current(fixture("current_running_v2.xml"), context, probe)
    fault = parse_sample(fixture("condition_fault_v2.xml"), context, probe)
    rolled = parse_sample(fixture("gap_buffer_roll_v2.xml"), context, probe)
    tracker = SequenceTracker()
    tracker.check(context.source_id, current.header)
    tracker.check(context.source_id, fault.header)
    gap = tracker.check(context.source_id, rolled.header)
    assert gap is not None

    normal = current.conditions[0]
    normalized = conditions_unknown_on_gap(
        fault.conditions,
        gap,
        {normal.item_key: condition_snapshot(normal)},
    )[0]
    assert normalized.epoch_policy == "falsify"
    assert normalized.retain_as_last_ok is True
    assert normalized.state == "unknown"
    assert normalized.severity == "unavailable"
    assert normalized.transition == "unavailable"
    assert normalized.last_ok["severity"] == "normal"
    assert normalized.last_ok["src_seq"] == 4
