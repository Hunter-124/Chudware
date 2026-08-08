from __future__ import annotations

from pathlib import Path

from services.manufacturing.monitor.replay import decode_captured_transcript, replay_transcript

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "mfg"
    / "monitor"
    / "mtconnect"
    / "replay_capture.txt"
)


def test_replay_is_byte_deterministic_and_uses_synthetic_monotonic_time() -> None:
    transcript = FIXTURE.read_text(encoding="utf-8")
    first = replay_transcript(transcript)
    second = replay_transcript(transcript)

    assert first.canonical_json() == second.canonical_json()
    assert [event.t_mono_ns for event in first.events] == [
        0,
        1_000_000_000,
        2_000_000_000,
        3_000_000_000,
        4_000_000_000,
    ]
    assert [frame.mono_ns for frame in decode_captured_transcript(transcript)] == [
        event.t_mono_ns for event in first.events
    ]


def test_replay_forces_simulated_source_even_for_a_remote_endpoint() -> None:
    result = replay_transcript(FIXTURE.read_text(encoding="utf-8"))

    assert result.observations
    assert result.conditions
    assert {row.src for row in result.observations} == {"simulated"}
    assert {row.src for row in result.conditions} == {"simulated"}
    assert {row.origin for row in result.observations} == {"replay_fixture"}
    assert all(row.evidence_eligible is False for row in result.observations)
    assert all(row.seq is None for row in (*result.observations, *result.conditions))


def test_replay_gap_is_unknown_and_does_not_promote_retained_value() -> None:
    result = replay_transcript(FIXTURE.read_text(encoding="utf-8"))
    final_event = result.events[-1]
    row = final_event.document.observations[0]

    assert final_event.gap["reason"] == "buffer_roll"
    assert final_event.gap["invalidation_class"] == "falsified"
    assert row.state == "unknown"
    assert row.gap is True
    assert row.v_num is row.v_text is row.v_json is None
    assert row.last_ok["v_num"] == 12.5


def test_replay_condition_transitions_retain_last_ok_without_a_command_surface() -> None:
    result = replay_transcript(FIXTURE.read_text(encoding="utf-8"))
    conditions = result.conditions

    assert [row.transition for row in conditions] == ["cleared", "raised", "cleared"]
    fault = conditions[1]
    assert fault.severity == "fault"
    assert fault.state == "unknown"
    assert fault.epoch_policy == "falsify"
    assert fault.retain_as_last_ok is True
    assert fault.last_ok["severity"] == "normal"
    assert conditions[2].severity == "normal"
    assert conditions[2].state == "ok"
