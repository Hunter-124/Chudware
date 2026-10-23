"""Logging middleware provider — wraps any Provider to emit ai_artifact telemetry.

This is a transparent decorator that conforms to contracts/providers.md (v1).  It
delegates every call to an inner provider and, around complete_ex, captures the
request/response bodies as sidecars and emits an ai_artifact event reusing the
helpers from services/agent/loop.py.

Design decisions:

1. TRANSPARENT PROXY — name, model, supports_vision, offline_safe, base_url are all
   forwarded so config.py's assert_offline_safe() continues to see the real provider
   attributes.  Wrap AFTER the offline assert (get_provider() already does that).

2. DOUBLE-EMIT AVOIDANCE — loop.py already emits ai_artifact at IP-1 post-build with
   verifier_scores.  Set suppress_emit=True (the default for IP-1) to write sidecars
   only; the caller (loop.py) reads sidecar_paths and passes them as input_ref /
   output_ref.

3. CONSENT GATES SIDECAR WRITES — at meta/off consent tiers, request/response bodies
   are NOT written (empty refs returned). Metadata fields (hash, latency) remain.

4. OVERHEAD — body serialization is O(message bytes). On typical CAD design calls
   (~8 KB prompt) the path is sub-millisecond.  Large image payloads can approach
   10 ms; the DoD threshold is honoured because image bytes are B64 already inside
   the provider, we only hash + optionally write the neutral message dict (not the
   base64 wire form).  Benchmark in test_logging_provider.py.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .base import Provider

# ---------------------------------------------------------------------------
# Context-var stash so the loop.py caller can recover sidecar paths without
# changing the complete_ex return signature (which is frozen by providers/1).
# ---------------------------------------------------------------------------
_last_sidecar_paths: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "lp_last_sidecar_paths", default={}
)


def get_last_sidecar_paths() -> dict:
    """Return {input_ref, output_ref} written by the last complete_ex call on this thread.

    Callers (loop.py) use this to pass sidecar refs into their own _emit_ai_artifact
    call instead of emitting a second event.  Empty strings if no sidecars were written
    (off/meta consent or body write disabled).
    """
    return dict(_last_sidecar_paths.get())


# ---------------------------------------------------------------------------
# Type alias matching services/agent/loop.py's EventFn
# ---------------------------------------------------------------------------
EventFn = Callable[[dict], None]


class LoggingProvider(Provider):
    """Transparent logging wrapper around any Provider.

    Parameters
    ----------
    inner:
        The real provider to delegate to.
    on_event:
        The EventFn sink (same signature as loop.py's on_event callback).
    sidecar_dir:
        Directory to write request/response sidecar JSONL files.  Created on first
        write.  If None, defaults to XDG_DATA_HOME/Chudware/telemetry/sidecars.
    insertion_point:
        The insertion-point tag for this call site (e.g. "IP-12").  Default "IP-1".
    suppress_emit:
        If True, write sidecars but do NOT call _emit_ai_artifact — the caller owns
        the emit.  Set True for IP-1 (loop.py emits post-build with verifier_scores).
        Set False for IP-12/18/20 where no downstream emitter exists.
    consent_fn:
        Zero-arg callable returning the current consent tier ("off"|"meta"|"full").
        Defaults to reading the live config.  Injected for tests.
    run_id:
        Opaque session identifier passed into ai_artifact.  Defaults to pid+time.
    """

    def __init__(
        self,
        inner: Provider,
        on_event: EventFn | None = None,
        *,
        sidecar_dir: Path | None = None,
        insertion_point: str = "IP-1",
        suppress_emit: bool = True,
        consent_fn: Callable[[], str] | None = None,
        run_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._on_event = on_event
        self._sidecar_dir = Path(sidecar_dir) if sidecar_dir else _default_sidecar_dir()
        self._insertion_point = insertion_point
        self._suppress_emit = suppress_emit
        self._consent_fn = consent_fn or _default_consent
        self._run_id = run_id or f"pid{os.getpid()}_{int(time.time())}"
        self._call_counter = 0

    # ------------------------------------------------------------------
    # Transparent attribute proxying (critical for offline-safety checks)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:  # type: ignore[override]
        return getattr(self._inner, "name", "base")

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unknown")

    @property
    def supports_vision(self) -> bool:  # type: ignore[override]
        return getattr(self._inner, "supports_vision", True)

    @property
    def offline_safe(self) -> bool:
        return getattr(self._inner, "offline_safe", False)

    @property
    def base_url(self) -> str | None:
        return getattr(self._inner, "base_url", None)

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]

    def complete_ex(
        self, system: str, messages: list[dict], max_tokens: int = 8000
    ) -> tuple[str, str]:
        consent = self._consent_fn()
        t0 = time.monotonic()
        text, thinking = self._inner.complete_ex(system, messages, max_tokens=max_tokens)
        latency_ms = (time.monotonic() - t0) * 1000.0

        self._call_counter += 1
        idx = self._call_counter

        # Compute prompt hash from all user message text (first 16 hex chars of SHA-256).
        prompt_text = _extract_prompt_text(messages)
        prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]

        # Write sidecars only at full consent — body must not leave machine at meta/off.
        input_ref = output_ref = ""
        if consent == "full":
            input_ref, output_ref = self._write_sidecars(
                idx, system, messages, text, thinking
            )

        # Stash sidecar paths so caller (loop.py at IP-1) can read them.
        _last_sidecar_paths.set({"input_ref": input_ref, "output_ref": output_ref})

        if not self._suppress_emit:
            _emit_artifact(
                on_event=self._on_event,
                run_id=self._run_id,
                call_index=idx,
                insertion_point=self._insertion_point,
                provider_name=self.name,
                model_name=self.model,
                prompt_hash=prompt_hash,
                input_ref=input_ref,
                output_ref=output_ref,
                latency_ms=latency_ms,
                consent=consent,
            )

        return text, thinking

    # ------------------------------------------------------------------
    # Sidecar I/O
    # ------------------------------------------------------------------

    def _write_sidecars(
        self,
        idx: int,
        system: str,
        messages: list[dict],
        text: str,
        thinking: str,
    ) -> tuple[str, str]:
        """Write request/response JSONL sidecars; return (input_ref, output_ref) paths."""
        try:
            self._sidecar_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{self._run_id}_{idx:04d}"

            req = {"system": system, "messages": messages}
            inp = self._sidecar_dir / f"{stem}_req.jsonl"
            inp.write_text(json.dumps(req, ensure_ascii=False) + "\n", encoding="utf-8")

            resp = {"text": text, "thinking": thinking}
            out = self._sidecar_dir / f"{stem}_resp.jsonl"
            out.write_text(json.dumps(resp, ensure_ascii=False) + "\n", encoding="utf-8")

            return str(inp), str(out)
        except Exception:  # noqa: BLE001
            # Never break the model call because of a logging failure.
            return "", ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_sidecar_dir() -> Path:
    # services.paths exports ROOT (the %APPDATA%/Chudware | XDG data root), not DATA —
    # the old attribute reference made the default sidecar dir a runtime AttributeError
    # on every platform (callers had to pass sidecar_dir explicitly).
    import services.paths as _paths  # type: ignore[import]
    return _paths.ROOT / "telemetry" / "sidecars"


def _default_consent() -> str:
    try:
        from services.agent import config as _cfg  # type: ignore[import]
        return _cfg.consent_tier(_cfg.load_config())
    except Exception:  # noqa: BLE001
        return "off"


def _extract_prompt_text(messages: list[dict]) -> str:
    """Concatenate text from the last user message for the prompt hash."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return "".join(
                p.get("text", "") for p in msg.get("content", []) if p.get("type") == "text"
            )
    return ""


def _emit_artifact(
    *,
    on_event: EventFn | None,
    run_id: str,
    call_index: int,
    insertion_point: str,
    provider_name: str,
    model_name: str,
    prompt_hash: str,
    input_ref: str,
    output_ref: str,
    latency_ms: float,
    consent: str,
) -> None:
    """Emit ai_artifact via the loop.py _emit helper pattern."""
    if on_event is None:
        return
    artifact_id = hashlib.sha256(
        (run_id + str(call_index)).encode()
    ).hexdigest()[:8]
    event: dict[str, Any] = {
        "kind": "ai_artifact",
        "artifact_id": artifact_id,
        "run_id": run_id,
        "insertion_point": insertion_point,
        "model": {
            "provider": provider_name,
            "model": model_name,
            "adapter": None,
        },
        "prompt_hash": prompt_hash,
        "input_ref": input_ref,
        "output_ref": output_ref,
        "base_revision": 0,
        "verifier_scores": {},
        "latency_ms": latency_ms,
        "consent": consent,
    }
    try:
        on_event(event)
    except Exception:  # noqa: BLE001
        pass


def wrap(
    provider: Provider,
    on_event: EventFn | None = None,
    *,
    insertion_point: str = "IP-1",
    suppress_emit: bool = True,
    sidecar_dir: Path | None = None,
    consent_fn: Callable[[], str] | None = None,
    run_id: str | None = None,
) -> "LoggingProvider":
    """Convenience factory: wrap *provider* in a LoggingProvider.

    By default wraps for IP-1 with suppress_emit=True (caller owns the emission).
    Pass insertion_point="IP-12" and suppress_emit=False for non-loop call sites.
    """
    return LoggingProvider(
        inner=provider,
        on_event=on_event,
        sidecar_dir=sidecar_dir,
        insertion_point=insertion_point,
        suppress_emit=suppress_emit,
        consent_fn=consent_fn,
        run_id=run_id,
    )
