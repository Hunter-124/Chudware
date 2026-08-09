from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from typing import Any

from .journal import Journal
from .model import AttentionItem, DeliveryRecord, more_severe


ATTENTION_KIND = "mfg.attention"


class AttentionStore:
    """Journal-authored attention lifecycle with a replay-only in-memory projection."""

    def __init__(self, journal: Journal, *, replay: bool = True) -> None:
        self.journal = journal
        self._lock = threading.RLock()
        self._items: dict[str, AttentionItem] = {}
        self._applied_seqs: set[int] = set()
        if replay:
            self.replay()

    def replay(self) -> None:
        for row in self.journal.replay():
            self.apply_record(row)

    def apply_record(self, row: dict[str, Any]) -> None:
        seq = int(row.get("seq", 0))
        if seq <= 0:
            raise ValueError("attention journal record requires a positive seq")
        kind = row.get("kind", row.get("event", ""))
        if kind != ATTENTION_KIND:
            return
        body = row.get("body", {})
        if not isinstance(body, dict):
            raise ValueError("attention journal body must be an object")
        with self._lock:
            if seq in self._applied_seqs:
                return
            action = body.get("action")
            item_id = str(body.get("id", row.get("subject", "")))
            if action == "raised":
                self._items[item_id] = AttentionItem(
                    id=item_id,
                    severity=str(body["severity"]),
                    device=str(body.get("device", "")),
                    run=str(body.get("run", "")),
                    reason=str(body["reason"]),
                    why=str(body["why"]),
                    raised_seq=seq,
                    occurrence_count=max(1, int(body.get("occurrence_count", 1))),
                )
            elif action == "occurred":
                item = self._require(item_id)
                item.occurrence_count += max(1, int(body.get("count", 1)))
                incoming = str(body.get("severity", item.severity))
                item.severity = more_severe(item.severity, incoming)
                if body.get("why"):
                    item.why = str(body["why"])
            elif action == "acknowledged":
                item = self._require(item_id)
                item.acknowledged_by = str(body["by"])
                item.acknowledged_seq = seq
            elif action == "resolved":
                self._require(item_id).resolved_seq = seq
            elif action == "delivery":
                item = self._require(item_id)
                item.deliveries.append(
                    DeliveryRecord(
                        channel=str(body["channel"]),
                        attempt=int(body["attempt"]),
                        status=str(body["status"]),
                        seq=seq,
                        error=str(body["error"]) if body.get("error") else None,
                    )
                )
            else:
                raise ValueError(f"unsupported attention journal action: {action}")
            self._applied_seqs.add(seq)

    def raise_attention(
        self,
        *,
        severity: str,
        device: str,
        run: str,
        reason: str,
        why: str,
    ) -> tuple[AttentionItem, bool]:
        if not reason.strip() or not why.strip():
            raise ValueError("attention reason and why must be non-empty")
        with self._lock:
            existing = next(
                (
                    item
                    for item in self._items.values()
                    if item.open
                    and item.device == device
                    and item.run == run
                    and item.reason == reason
                ),
                None,
            )
            if existing is not None:
                body = {
                    "action": "occurred",
                    "id": existing.id,
                    "severity": severity,
                    "why": why,
                    "count": 1,
                }
                seq = self.journal.append(ATTENTION_KIND, existing.id, body)
                self.apply_record({"seq": seq, "kind": ATTENTION_KIND, "subject": existing.id, "body": body})
                return existing, False

            item_id = f"attn:{uuid.uuid4().hex}"
            body = {
                "action": "raised",
                "id": item_id,
                "severity": severity,
                "device": device,
                "run": run,
                "reason": reason,
                "why": why,
                "occurrence_count": 1,
            }
            seq = self.journal.append(ATTENTION_KIND, item_id, body)
            self.apply_record({"seq": seq, "kind": ATTENTION_KIND, "subject": item_id, "body": body})
            return self._items[item_id], True

    def acknowledge(self, item_id: str, actor: str) -> AttentionItem:
        if not actor.strip():
            raise ValueError("acknowledgement actor must be non-empty")
        with self._lock:
            item = self._require(item_id)
            if item.acknowledged_seq is not None or item.resolved_seq is not None:
                return item
            body = {"action": "acknowledged", "id": item_id, "by": actor}
            seq = self.journal.append(ATTENTION_KIND, item_id, body)
            self.apply_record({"seq": seq, "kind": ATTENTION_KIND, "subject": item_id, "body": body})
            return item

    def resolve(self, item_id: str) -> AttentionItem:
        with self._lock:
            item = self._require(item_id)
            if item.resolved_seq is not None:
                return item
            body = {"action": "resolved", "id": item_id}
            seq = self.journal.append(ATTENTION_KIND, item_id, body)
            self.apply_record({"seq": seq, "kind": ATTENTION_KIND, "subject": item_id, "body": body})
            return item

    def record_delivery(
        self,
        item_id: str,
        *,
        channel: str,
        attempt: int,
        status: str,
        error: str | None = None,
    ) -> DeliveryRecord:
        body: dict[str, Any] = {
            "action": "delivery",
            "id": item_id,
            "channel": channel,
            "attempt": attempt,
            "status": status,
        }
        if error:
            body["error"] = error
        with self._lock:
            self._require(item_id)
            seq = self.journal.append(ATTENTION_KIND, item_id, body)
            self.apply_record({"seq": seq, "kind": ATTENTION_KIND, "subject": item_id, "body": body})
            return self._items[item_id].deliveries[-1]

    def get(self, item_id: str) -> AttentionItem:
        with self._lock:
            return deepcopy(self._require(item_id))

    def open_items(self) -> list[AttentionItem]:
        with self._lock:
            return deepcopy(sorted(
                (item for item in self._items.values() if item.open),
                key=lambda item: item.raised_seq,
                reverse=True,
            ))

    def all_items(self) -> list[AttentionItem]:
        with self._lock:
            return deepcopy(sorted(self._items.values(), key=lambda item: item.raised_seq, reverse=True))

    def _require(self, item_id: str) -> AttentionItem:
        try:
            return self._items[item_id]
        except KeyError:
            raise KeyError(f"unknown attention item: {item_id}") from None
