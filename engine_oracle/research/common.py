"""Shared helpers for ``engine/research``: the v1 answer envelope, timestamps, and the
offline gate. Contract: ``contracts/research_tools.md`` §1 (envelope), §5 (offline),
§6 (degradation).

Kept dependency-light on purpose: no network, no heavy imports at module load. The
offline check lazily imports the agent config so ``engine`` never hard-depends on
``agent`` (avoids an import cycle — ``agent.config`` imports ``engine.paths``).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

# confidence enum — contract §1.1
HIGH = "high"
MEDIUM = "medium"
LIMITED = "limited"
BUNDLED = "bundled"
OFFLINE = "offline"


def now_ts() -> str:
    """Current UTC instant as ISO-8601 with a ``Z`` suffix (contract ``fetched_ts``)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(
    answer: Any,
    *,
    units: str | None = None,
    source_url: str | None = None,
    quote: str | None = None,
    fetched_ts: str | None = None,
    confidence: str = HIGH,
    cached: bool = False,
    **extra: Any,
) -> dict:
    """Build a §1 success envelope. Helper-specific fields pass through ``extra`` and
    sit *alongside* the envelope keys (never renaming them)."""
    env: dict[str, Any] = {
        "ok": True,
        "answer": answer,
        "units": units,
        "source_url": source_url,
        "quote": quote,
        "fetched_ts": fetched_ts or now_ts(),
        "confidence": confidence,
        "cached": cached,
    }
    env.update(extra)
    return env


def fail(reason: str, hint: str) -> dict:
    """Contract §6 degradation shape — never raise across the tool boundary."""
    return {"ok": False, "reason": reason, "hint": hint}


def is_offline() -> bool:
    """True when offline/ITAR mode is on — the SAME flag the agent enforces
    (``services/agent/config.py`` ``offline_enabled`` / ``assert_offline_safe``).

    Lazily imported to avoid an ``engine`` -> ``agent`` import cycle; falls back to
    reading ``config.json`` directly if the agent package is unavailable. Fails
    *closed to online* only when nothing is configured (default is online)."""
    try:
        from agent import config as _cfg  # type: ignore

        return bool(_cfg.offline_enabled())
    except Exception:  # noqa: BLE001 — agent package may not be importable in all contexts
        try:
            import json

            from engine import paths

            if paths.CONFIG.exists():
                data = json.loads(paths.CONFIG.read_text(encoding="utf-8"))
                return bool(data.get("offline_mode", False))
        except Exception:  # noqa: BLE001
            pass
        return False
