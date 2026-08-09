"""Durable manufacturing attention lifecycle and bounded notification delivery."""

from .channels import (
    ChannelPolicy,
    DeliveryManager,
    DesktopChannel,
    InAppChannel,
    WebhookChannel,
    redact_text,
    safe_links,
    webhook_payload,
)
from .journal import JsonlJournal, MemoryJournal
from .model import AttentionItem, DeliveryRecord
from .service import AttentionCause, AttentionService, cause_from_event
from .store import AttentionStore

__all__ = [
    "AttentionCause",
    "AttentionItem",
    "AttentionService",
    "AttentionStore",
    "ChannelPolicy",
    "DeliveryManager",
    "DeliveryRecord",
    "DesktopChannel",
    "InAppChannel",
    "JsonlJournal",
    "MemoryJournal",
    "WebhookChannel",
    "cause_from_event",
    "redact_text",
    "safe_links",
    "webhook_payload",
]
