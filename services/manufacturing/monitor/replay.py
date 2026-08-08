"""Deterministic, transport-free replay of captured monitoring transcripts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from typing import Any, Iterable, Mapping

from .mtconnect import MalformedDocumentRefusal, ParsedDocument, parse_mtconnect
from .normalize import (
    ConditionRow,
    DataItemDefinition,
    EndpointContext,
    ObservationRow,
    SequenceTracker,
    condition_snapshot,
    conditions_unknown_on_gap,
    observations_unknown_on_gap,
    value_snapshot,
)


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    mono_ns: int
    document_kind: str
    payload: str
    endpoint: str
    label: str
    origin: str = "replay_fixture"


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    frame_index: int
    t_mono_ns: int
    document: ParsedDocument
    gap: Mapping[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    events: tuple[ReplayEvent, ...]

    @property
    def observations(self) -> tuple[ObservationRow, ...]:
        return tuple(row for event in self.events for row in event.document.observations)

    @property
    def conditions(self) -> tuple[ConditionRow, ...]:
        return tuple(row for event in self.events for row in event.document.conditions)

    def to_dict(self) -> dict[str, Any]:
        return {"events": [event.to_dict() for event in self.events]}

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode_captured_transcript(transcript: str) -> tuple[CapturedFrame, ...]:
    """Decode readable ``@frame JSON`` / XML / ``@end`` capture blocks."""

    frames: list[CapturedFrame] = []
    metadata: dict[str, Any] | None = None
    payload_lines: list[str] = []
    for line_number, line in enumerate(transcript.splitlines(), start=1):
        if line.startswith("@frame "):
            if metadata is not None:
                raise MalformedDocumentRefusal(f"nested replay frame at transcript line {line_number}")
            try:
                decoded = json.loads(line[len("@frame ") :])
            except json.JSONDecodeError as exc:
                raise MalformedDocumentRefusal(f"invalid frame metadata at line {line_number}: {exc}") from exc
            if not isinstance(decoded, dict):
                raise MalformedDocumentRefusal(f"frame metadata at line {line_number} must be an object")
            metadata = decoded
            payload_lines = []
            continue
        if line == "@end":
            if metadata is None:
                raise MalformedDocumentRefusal(f"orphan @end at transcript line {line_number}")
            required = {"mono_ns", "kind", "endpoint", "label"}
            missing = sorted(required.difference(metadata))
            if missing:
                raise MalformedDocumentRefusal(f"frame metadata missing: {', '.join(missing)}")
            mono_ns = metadata["mono_ns"]
            if not isinstance(mono_ns, int) or isinstance(mono_ns, bool) or mono_ns < 0:
                raise MalformedDocumentRefusal("frame mono_ns must be a non-negative integer")
            frames.append(
                CapturedFrame(
                    mono_ns=mono_ns,
                    document_kind=str(metadata["kind"]),
                    payload="\n".join(payload_lines),
                    endpoint=str(metadata["endpoint"]),
                    label=str(metadata["label"]),
                    origin=str(metadata.get("origin", "replay_fixture")),
                )
            )
            metadata = None
            payload_lines = []
            continue
        if metadata is None:
            if line.strip():
                raise MalformedDocumentRefusal(f"content outside a replay frame at line {line_number}")
        else:
            payload_lines.append(line)
    if metadata is not None:
        raise MalformedDocumentRefusal("unterminated replay frame")
    if not frames:
        raise MalformedDocumentRefusal("replay transcript contains no frames")
    previous = -1
    for frame in frames:
        if frame.mono_ns < previous:
            raise MalformedDocumentRefusal("replay frame monotonic times regress")
        previous = frame.mono_ns
    return tuple(frames)


def _condition_transition(row: ConditionRow, previous: ConditionRow | None) -> str:
    if row.severity == "unavailable":
        return "unavailable"
    if row.severity == "normal":
        return "cleared"
    if previous is None or previous.severity == "normal":
        return "raised"
    return "changed"


def replay_frames(frames: Iterable[CapturedFrame]) -> ReplayResult:
    tracker = SequenceTracker()
    definitions: dict[str, DataItemDefinition] = {}
    last_good_observations: dict[str, Mapping[str, Any]] = {}
    last_good_conditions: dict[str, Mapping[str, Any]] = {}
    previous_conditions: dict[str, ConditionRow] = {}
    events: list[ReplayEvent] = []
    previous_mono = -1

    for frame_index, frame in enumerate(frames):
        if frame.mono_ns < previous_mono:
            raise MalformedDocumentRefusal("replay frame monotonic times regress")
        previous_mono = frame.mono_ns
        origin = "twin_trace" if frame.origin == "twin_trace" else "replay_fixture"
        context = EndpointContext(
            endpoint=frame.endpoint,
            configured_label=frame.label,
            origin=origin,
            endpoint_is_physical=False,
            manifest_virtual=True,
        )
        document = parse_mtconnect(
            frame.payload,
            context,
            definitions,
            document_kind=frame.document_kind,
        )
        if document.document_kind == "probe":
            definitions.update(document.data_item_map())
            events.append(ReplayEvent(frame_index, frame.mono_ns, document, None))
            continue

        gap = tracker.check(context.source_id, document.header) if document.header is not None else None
        observations = document.observations
        conditions = document.conditions
        if gap is not None:
            observations = observations_unknown_on_gap(observations, gap, last_good_observations)
            conditions = conditions_unknown_on_gap(conditions, gap, last_good_conditions)
        else:
            normalized_observations: list[ObservationRow] = []
            for row in observations:
                if row.state == "ok":
                    last_good_observations[row.item_key] = value_snapshot(row)
                elif row.item_key in last_good_observations:
                    row = replace(row, last_ok=last_good_observations[row.item_key])
                normalized_observations.append(row)
            observations = tuple(normalized_observations)

            normalized_conditions: list[ConditionRow] = []
            for row in conditions:
                previous = previous_conditions.get(row.item_key)
                row = replace(row, transition=_condition_transition(row, previous))
                if row.state == "ok":
                    last_good_conditions[row.item_key] = condition_snapshot(row)
                elif row.item_key in last_good_conditions:
                    row = replace(row, last_ok=last_good_conditions[row.item_key])
                previous_conditions[row.item_key] = row
                normalized_conditions.append(row)
            conditions = tuple(normalized_conditions)

        document = replace(document, observations=observations, conditions=conditions)
        events.append(ReplayEvent(frame_index, frame.mono_ns, document, gap.to_dict() if gap else None))
    return ReplayResult(tuple(events))


def replay_transcript(transcript: str) -> ReplayResult:
    return replay_frames(decode_captured_transcript(transcript))
