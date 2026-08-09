from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .channels import DeliveryManager
from .model import AttentionItem
from .store import AttentionStore


@dataclass(frozen=True, slots=True)
class AttentionCause:
    severity: str
    device: str
    run: str
    reason: str
    why: str
    links: tuple[str, ...] = ()


def _text(body: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = body.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _identity(event: Mapping[str, Any], body: Mapping[str, Any], prefix: str) -> str:
    value = _text(body, f"{prefix}_id", prefix)
    if value:
        return value
    subject = str(event.get("subject", ""))
    return subject if subject.startswith(f"{prefix}:") else ""


def cause_from_event(event: Mapping[str, Any]) -> AttentionCause | None:
    """Convert authoritative mfg.* facts into an attention cause.

    Producers may attach an explicit ``body.attention`` object. Safety-relevant
    gateway facts also have fixed mappings so alarms are not dependent on prose.
    Benign mfg.* telemetry intentionally produces no attention item.
    """
    kind = str(event.get("event", event.get("kind", "")))
    if not kind.startswith("mfg."):
        return None
    raw_body = event.get("body", {})
    body: Mapping[str, Any] = raw_body if isinstance(raw_body, Mapping) else {}
    device = _identity(event, body, "device")
    run = _identity(event, body, "run")

    explicit = body.get("attention")
    if isinstance(explicit, Mapping):
        links = explicit.get("links", ())
        if not isinstance(links, Sequence) or isinstance(links, (str, bytes)):
            links = ()
        return AttentionCause(
            severity=str(explicit.get("severity", "warning")),
            device=str(explicit.get("device", device)),
            run=str(explicit.get("run", run)),
            reason=str(explicit.get("reason", kind.removeprefix("mfg."))),
            why=str(explicit.get("why", _text(body, "why", "message") or kind)),
            links=tuple(str(link) for link in links),
        )

    if kind == "mfg.attention" and body.get("action") in {None, "raise", "raised"}:
        links = body.get("links", ())
        if not isinstance(links, Sequence) or isinstance(links, (str, bytes)):
            links = ()
        return AttentionCause(
            severity=str(body.get("severity", "warning")),
            device=device,
            run=run,
            reason=_text(body, "reason") or "machine_attention",
            why=_text(body, "why", "message") or "The machine requires attention.",
            links=tuple(str(link) for link in links),
        )

    if kind == "mfg.device.alarm":
        return AttentionCause(
            "critical",
            device,
            run,
            "controller_alarm",
            _text(body, "why", "message", "alarm") or "The controller reported an alarm.",
        )
    if kind == "mfg.device.foreign_client_detected":
        return AttentionCause(
            "critical",
            device,
            run,
            "foreign_client_detected",
            _text(body, "why", "message") or "Another client has control of the machine.",
        )
    if kind == "mfg.run.command_ack" and body.get("state") == "unknown":
        return AttentionCause(
            "critical",
            device,
            run,
            "command_outcome_unknown",
            _text(body, "why", "message") or "The command outcome is unknown; do not retry.",
        )
    if kind == "mfg.run.state":
        state = _text(body, "state")
        if state in {"faulted", "abandoned"}:
            return AttentionCause(
                "critical" if state == "faulted" else "warning",
                device,
                run,
                f"run_{state}",
                _text(body, "why", "message") or f"The run entered {state} state.",
            )
    if kind == "mfg.perception_frame_health":
        state = _text(body, "state", "status") or "unknown"
        if state not in {"ok", "healthy", "current"}:
            return AttentionCause(
                "warning",
                device,
                run,
                f"perception_{state}",
                _text(body, "why", "message") or f"Perception frame health is {state}.",
            )
    return None


class AttentionService:
    def __init__(self, store: AttentionStore, delivery: DeliveryManager | None = None) -> None:
        self.store = store
        self.delivery = delivery

    def handle_event(self, event: Mapping[str, Any]) -> AttentionItem | None:
        kind = str(event.get("event", event.get("kind", "")))
        body = event.get("body", {})
        if kind == "mfg.attention" and isinstance(body, Mapping):
            action = body.get("action")
            item_id = str(body.get("id", event.get("subject", "")))
            if action in {"ack", "acknowledged"}:
                return self.store.acknowledge(item_id, str(body.get("by", "")))
            if action in {"resolve", "resolved"}:
                return self.store.resolve(item_id)

        cause = cause_from_event(event)
        if cause is None:
            return None
        item, created = self.store.raise_attention(
            severity=cause.severity,
            device=cause.device,
            run=cause.run,
            reason=cause.reason,
            why=cause.why,
        )
        if created and self.delivery is not None:
            self.delivery.deliver(item, links=cause.links)
        return self.store.get(item.id)

    def acknowledge(self, item_id: str, actor: str) -> AttentionItem:
        return self.store.acknowledge(item_id, actor)

    def resolve(self, item_id: str) -> AttentionItem:
        return self.store.resolve(item_id)

    def list_open(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.store.open_items()]

    def list_all(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.store.all_items()]
