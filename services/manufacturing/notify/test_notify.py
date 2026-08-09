from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import pytest

from services.manufacturing.notify import (
    AttentionService,
    AttentionStore,
    ChannelPolicy,
    DeliveryManager,
    DesktopChannel,
    InAppChannel,
    JsonlJournal,
    MemoryJournal,
    WebhookChannel,
)


class RecordingChannel:
    def __init__(self, name: str = "recording", *, failures: int = 0) -> None:
        self.name = name
        self.failures = failures
        self.calls: list[dict[str, Any]] = []

    def deliver(self, payload: Mapping[str, Any]) -> None:
        self.calls.append(dict(payload))
        if len(self.calls) <= self.failures:
            raise RuntimeError("dead channel token=should-not-leak")


def alarm(*, reason: str = "controller_alarm", why: str = "Hard limit opened") -> dict[str, Any]:
    return {
        "event": "mfg.device.alarm",
        "subject": "dev:mill:abc",
        "body": {
            "device_id": "dev:mill:abc",
            "run_id": "run:01",
            "attention": {"severity": "critical", "reason": reason, "why": why},
        },
    }


def test_raise_acknowledge_resolve_lifecycle() -> None:
    store = AttentionStore(MemoryJournal())
    item, created = store.raise_attention(
        severity="critical",
        device="dev:mill:abc",
        run="run:01",
        reason="controller_alarm",
        why="Hard limit opened",
    )
    assert created and store.open_items()[0].id == item.id

    acknowledged = store.acknowledge(item.id, "hunter@workstation")
    assert acknowledged.acknowledged_by == "hunter@workstation"
    assert acknowledged.acknowledged_seq is not None
    assert store.open_items() == []

    resolved = store.resolve(item.id)
    assert resolved.resolved_seq is not None
    assert resolved.resolved_seq > resolved.acknowledged_seq


def test_repeating_cause_deduplicates_and_does_not_storm_channels() -> None:
    journal = MemoryJournal()
    store = AttentionStore(journal)
    channel = RecordingChannel()
    service = AttentionService(
        store,
        DeliveryManager(store, [channel], sleeper=lambda _: None),
    )

    first = service.handle_event(alarm())
    second = service.handle_event(alarm(why="Hard limit remains open"))

    assert first is not None and second is not None
    assert first.id == second.id
    assert second.occurrence_count == 2
    assert second.why == "Hard limit remains open"
    assert len(channel.calls) == 1
    assert len(store.open_items()) == 1


def test_restart_replays_durable_journal_fixture() -> None:
    fixture = Path(__file__).with_name("fixtures") / "restart_journal.jsonl"
    first = AttentionStore(JsonlJournal(fixture)).get("attn:fixture")
    restarted = AttentionStore(JsonlJournal(fixture)).get("attn:fixture")

    assert restarted.to_dict() == first.to_dict()
    assert restarted.raised_seq == 41
    assert restarted.occurrence_count == 2
    assert restarted.undelivered_channels == ["desktop"]
    assert restarted.open


def test_dead_channel_retries_then_surfaces_durable_failure() -> None:
    store = AttentionStore(MemoryJournal())
    dead = RecordingChannel("webhook", failures=20)
    manager = DeliveryManager(
        store,
        [dead],
        policies={"webhook": ChannelPolicy(max_attempts=3, base_retry_s=0, rate_limit=10)},
        sleeper=lambda _: None,
    )
    service = AttentionService(store, manager)

    item = service.handle_event(alarm())

    assert item is not None
    records = store.get(item.id).deliveries
    assert [row.status for row in records] == ["retrying", "retrying", "failed"]
    assert len(dead.calls) == 3
    assert store.get(item.id).undelivered_channels == ["webhook"]
    assert "should-not-leak" not in records[-1].error


def test_per_channel_rate_limit_is_visible_and_does_not_call_channel() -> None:
    now = [100.0]
    store = AttentionStore(MemoryJournal())
    channel = RecordingChannel("desktop")
    manager = DeliveryManager(
        store,
        [channel],
        policies={"desktop": ChannelPolicy(max_attempts=1, rate_limit=1, rate_window_s=60)},
        clock=lambda: now[0],
        sleeper=lambda _: None,
    )
    service = AttentionService(store, manager)

    first = service.handle_event(alarm(reason="alarm_a"))
    second = service.handle_event(alarm(reason="alarm_b"))

    assert first is not None and second is not None
    assert len(channel.calls) == 1
    second_item = store.get(second.id)
    assert second_item.deliveries[-1].status == "rate_limited"
    assert second_item.undelivered_channels == ["desktop"]


class _WebhookCapture(BaseHTTPRequestHandler):
    payloads: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("Content-Length", "0"))
        self.payloads.append(json.loads(self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return None


def test_webhook_uses_allowlist_redacts_secrets_and_never_carries_raw_media() -> None:
    _WebhookCapture.payloads.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookCapture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = AttentionStore(MemoryJournal())
        service = AttentionService(
            store,
            DeliveryManager(
                store,
                [WebhookChannel(f"http://127.0.0.1:{server.server_port}/attention")],
                policies={"webhook": ChannelPolicy(max_attempts=1)},
            ),
        )
        event = alarm(why="Camera failed; token=super-secret password=hunter2")
        event["body"]["attention"]["links"] = [
            "https://example.test/run/01?token=url-secret&view=attention",
            "data:image/png;base64,RAW-MEDIA",
        ]
        event["body"]["api_key"] = "body-secret"
        event["body"]["raw_media"] = "RAW-MEDIA"

        item = service.handle_event(event)
        assert item is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert len(_WebhookCapture.payloads) == 1
    payload = _WebhookCapture.payloads[0]
    encoded = json.dumps(payload)
    assert set(payload) == {
        "schema", "id", "severity", "device", "run", "reason", "why",
        "raised_seq", "occurrence_count", "links",
    }
    for forbidden in ("super-secret", "hunter2", "url-secret", "body-secret", "RAW-MEDIA"):
        assert forbidden not in encoded
    assert payload["links"] == [
        "https://example.test/run/01?token=%5BREDACTED%5D&view=attention"
    ]


def test_in_app_queue_and_desktop_adapter_are_real_bounded_channels() -> None:
    store = AttentionStore(MemoryJournal())
    in_app = InAppChannel(capacity=1)
    desktop = DesktopChannel(command=(sys.executable, "-c", "import sys; raise SystemExit(0)"))
    manager = DeliveryManager(
        store,
        [in_app, desktop],
        policies={
            "in_app": ChannelPolicy(max_attempts=1),
            "desktop": ChannelPolicy(max_attempts=1),
        },
    )
    item, _ = store.raise_attention(
        severity="warning", device="dev:printer:1", run="run:2",
        reason="filament", why="Load filament",
    )

    manager.deliver(item)

    queued = in_app.drain()
    assert len(queued) == 1 and queued[0]["id"] == item.id
    assert {row.channel: row.status for row in store.get(item.id).deliveries} == {
        "in_app": "delivered",
        "desktop": "delivered",
    }


def test_benign_non_attention_event_is_ignored() -> None:
    service = AttentionService(AttentionStore(MemoryJournal()))
    assert service.handle_event({"event": "mfg.device.snapshot", "body": {"state": "idle"}}) is None
    assert service.list_open() == []
