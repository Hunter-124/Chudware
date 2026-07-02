"""Shadow-mode comparison harness.

Two execution paths:

1. LIVE-PARALLEL — at call time, run the challenger provider alongside the incumbent
   in a background thread (never shown to user, rate-limited).

2. REPLAY-FROM-LOG — read a JSONL events log (ai_artifact events + sidecars) and
   replay each request against the challenger, scoring both with the T-H4 battery
   stub (GroundednessChecker protocol) and writing a comparison record.

Offline enforcement: the challenger is refused before construction when offline_mode
is enabled (same as primary provider — see contracts/providers.md §5).

Verifier scoring is a seam, not a dependency.  The Scorer protocol is injected; the
default stub returns {} (scores will be populated when T-H4 merges and a concrete
checker is wired in).
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

from .providers.base import Provider


# ---------------------------------------------------------------------------
# Verifier scoring seam (T-H4 protocol — injected, not imported concretely)
# ---------------------------------------------------------------------------

@runtime_checkable
class Scorer(Protocol):
    """Minimal protocol the T-H4 battery must satisfy for shadow scoring."""

    def score(self, output_text: str, *, context: dict | None = None) -> dict[str, float]:
        """Return a mapping of score_name -> float (0.0–1.0) for *output_text*."""
        ...


class _NullScorer:
    """Default stub when no concrete scorer is injected (T-H4 not yet merged)."""

    def score(self, output_text: str, *, context: dict | None = None) -> dict[str, float]:
        return {}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ShadowRecord:
    """A single comparison record written to the shadow log."""

    artifact_id: str
    insertion_point: str
    prompt_hash: str

    # Incumbent (primary)
    incumbent_provider: str
    incumbent_model: str
    incumbent_latency_ms: float
    incumbent_scores: dict[str, float]

    # Challenger (shadow)
    challenger_provider: str
    challenger_model: str
    challenger_latency_ms: float
    challenger_scores: dict[str, float]

    # Accept/reject comparison
    incumbent_accepted: bool = False
    challenger_accepted: bool = False

    # Optional: was this a replay or live-parallel call?
    mode: str = "live"  # "live" | "replay"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Rate limiter (token bucket, thread-safe)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Token-bucket rate limiter for shadow calls.

    Defaults to 10 calls/minute so a replay run doesn't exhaust API quota.
    """

    def __init__(self, calls_per_minute: float = 10.0) -> None:
        self._interval = 60.0 / max(calls_per_minute, 0.01)
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
            self._next_allowed = time.monotonic() + self._interval


# ---------------------------------------------------------------------------
# Shadow executor
# ---------------------------------------------------------------------------

class ShadowExecutor:
    """Runs a challenger provider in shadow mode against an incumbent.

    Usage (live-parallel mode):
        shadow = ShadowExecutor(challenger, on_record=write_fn)
        shadow.run_parallel(incumbent_result, system, messages,
                            artifact_id="abcdef12", insertion_point="IP-1",
                            incumbent_provider="anthropic", incumbent_model="claude-...",
                            incumbent_latency_ms=1200.0)

    Usage (replay mode):
        shadow = ShadowExecutor(challenger, on_record=write_fn)
        shadow.replay_log(events_path, sidecar_base_dir, max_calls=200)
    """

    def __init__(
        self,
        challenger: Provider,
        *,
        on_record: Callable[[ShadowRecord], None] | None = None,
        scorer: Scorer | None = None,
        rate_limiter: _RateLimiter | None = None,
        offline_mode: bool | None = None,
        shadow_log_path: Path | None = None,
    ) -> None:
        # Offline check: refuse cloud challenger before construction.
        if offline_mode is None:
            offline_mode = _load_offline_mode()
        if offline_mode:
            _assert_offline_safe_provider(challenger)

        self._challenger = challenger
        self._on_record = on_record or _noop_record
        self._scorer = scorer or _NullScorer()
        self._rl = rate_limiter or _RateLimiter(calls_per_minute=10.0)
        self._shadow_log = shadow_log_path
        self._bg_queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=50)
        self._bg_thread = threading.Thread(
            target=self._worker, daemon=True, name="shadow-executor"
        )
        self._bg_thread.start()

    # ------------------------------------------------------------------
    # Live-parallel mode
    # ------------------------------------------------------------------

    def run_parallel(
        self,
        incumbent_text: str,
        system: str,
        messages: list[dict],
        *,
        artifact_id: str,
        insertion_point: str,
        incumbent_provider: str,
        incumbent_model: str,
        incumbent_latency_ms: float,
        incumbent_scores: dict[str, float] | None = None,
        prompt_hash: str = "",
    ) -> None:
        """Queue a shadow call for background execution (non-blocking).

        The challenger call runs in the background thread so it never adds to
        the user-visible latency.  Results are delivered via on_record.
        """
        task = _ShadowTask(
            system=system,
            messages=messages,
            artifact_id=artifact_id,
            insertion_point=insertion_point,
            incumbent_text=incumbent_text,
            incumbent_provider=incumbent_provider,
            incumbent_model=incumbent_model,
            incumbent_latency_ms=incumbent_latency_ms,
            incumbent_scores=incumbent_scores or {},
            prompt_hash=prompt_hash,
        )
        try:
            self._bg_queue.put_nowait(lambda t=task: self._execute_task(t))
        except queue.Full:
            # Drop the shadow call rather than block the main thread.
            pass

    # ------------------------------------------------------------------
    # Replay-from-log mode
    # ------------------------------------------------------------------

    def replay_log(
        self,
        events_path: Path,
        sidecar_base_dir: Path | None = None,
        *,
        max_calls: int = 0,
    ) -> list[ShadowRecord]:
        """Replay ai_artifact events from *events_path*, running the challenger on each.

        Parameters
        ----------
        events_path:
            Path to a JSONL file of ai_artifact events (one JSON object per line).
        sidecar_base_dir:
            Directory to resolve relative sidecar paths.  Defaults to events_path.parent.
        max_calls:
            Cap on number of events to replay (0 = no cap).  Useful for smoke tests.

        Returns all ShadowRecord objects produced.
        """
        sidecar_base = sidecar_base_dir or events_path.parent
        records: list[ShadowRecord] = []
        events_path = Path(events_path)

        for i, event in enumerate(_iter_artifact_events(events_path)):
            if max_calls and i >= max_calls:
                break

            system, messages = _load_request_sidecar(event, sidecar_base)
            if system is None or messages is None:
                # No sidecar; synthesise a minimal request from the event metadata.
                system = ""
                messages = [{"role": "user", "content": [
                    {"type": "text", "text": f"(sidecar missing for {event.get('artifact_id', '?')})"}
                ]}]

            incumbent_scores = dict(event.get("verifier_scores") or {})

            record = self._run_challenger(
                system=system,
                messages=messages,
                artifact_id=event.get("artifact_id", f"replay_{i:04d}"),
                insertion_point=event.get("insertion_point", "IP-1"),
                incumbent_provider=event.get("model", {}).get("provider", "unknown"),
                incumbent_model=event.get("model", {}).get("model", "unknown"),
                incumbent_latency_ms=float(event.get("latency_ms") or 0.0),
                incumbent_scores=incumbent_scores,
                incumbent_text="",  # not stored in event; loaded from resp sidecar if present
                prompt_hash=event.get("prompt_hash", ""),
                mode="replay",
            )
            records.append(record)
            self._on_record(record)
            self._append_shadow_log(record)

        return records

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            fn = self._bg_queue.get()
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._bg_queue.task_done()

    def _execute_task(self, task: "_ShadowTask") -> None:
        self._rl.acquire()
        record = self._run_challenger(
            system=task.system,
            messages=task.messages,
            artifact_id=task.artifact_id,
            insertion_point=task.insertion_point,
            incumbent_text=task.incumbent_text,
            incumbent_provider=task.incumbent_provider,
            incumbent_model=task.incumbent_model,
            incumbent_latency_ms=task.incumbent_latency_ms,
            incumbent_scores=task.incumbent_scores,
            prompt_hash=task.prompt_hash,
            mode="live",
        )
        self._on_record(record)
        self._append_shadow_log(record)

    def _run_challenger(
        self,
        *,
        system: str,
        messages: list[dict],
        artifact_id: str,
        insertion_point: str,
        incumbent_text: str,
        incumbent_provider: str,
        incumbent_model: str,
        incumbent_latency_ms: float,
        incumbent_scores: dict[str, float],
        prompt_hash: str,
        mode: str = "live",
    ) -> ShadowRecord:
        t0 = time.monotonic()
        try:
            ch_text, _ = self._challenger.complete_ex(system, messages)
        except Exception:  # noqa: BLE001
            ch_text = ""
        ch_latency = (time.monotonic() - t0) * 1000.0

        ch_scores = self._scorer.score(ch_text, context={"insertion_point": insertion_point})
        inc_scores_computed = (
            incumbent_scores
            if incumbent_scores
            else self._scorer.score(incumbent_text, context={"insertion_point": insertion_point})
        )

        ch_provider = getattr(self._challenger, "name", "unknown")
        ch_model = getattr(self._challenger, "model", "unknown")

        # Accept = any score in the dict >= 0.5, or if dict is empty => True (no scorer).
        def _accepted(scores: dict) -> bool:
            if not scores:
                return True
            return any(v >= 0.5 for v in scores.values())

        return ShadowRecord(
            artifact_id=artifact_id,
            insertion_point=insertion_point,
            prompt_hash=prompt_hash,
            incumbent_provider=incumbent_provider,
            incumbent_model=incumbent_model,
            incumbent_latency_ms=incumbent_latency_ms,
            incumbent_scores=inc_scores_computed,
            challenger_provider=ch_provider,
            challenger_model=ch_model,
            challenger_latency_ms=ch_latency,
            challenger_scores=ch_scores,
            incumbent_accepted=_accepted(inc_scores_computed),
            challenger_accepted=_accepted(ch_scores),
            mode=mode,
        )

    def _append_shadow_log(self, record: ShadowRecord) -> None:
        if not self._shadow_log:
            return
        try:
            Path(self._shadow_log).parent.mkdir(parents=True, exist_ok=True)
            with open(self._shadow_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def drain(self, timeout: float = 30.0) -> None:
        """Wait for all queued background tasks to finish (useful in tests)."""
        self._bg_queue.join()


@dataclass
class _ShadowTask:
    system: str
    messages: list[dict]
    artifact_id: str
    insertion_point: str
    incumbent_text: str
    incumbent_provider: str
    incumbent_model: str
    incumbent_latency_ms: float
    incumbent_scores: dict[str, float]
    prompt_hash: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_artifact_events(path: Path) -> Iterator[dict]:
    """Yield ai_artifact events from a JSONL events file."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("kind") == "ai_artifact":
                yield obj


def _load_request_sidecar(
    event: dict, sidecar_base: Path
) -> tuple[str | None, list | None]:
    """Load the request sidecar referenced by *event*.

    Returns (system, messages) or (None, None) if the sidecar is absent/unreadable.
    """
    ref = event.get("input_ref", "")
    if not ref:
        return None, None
    p = Path(ref) if Path(ref).is_absolute() else sidecar_base / ref
    if not p.exists():
        return None, None
    try:
        first_line = p.read_text(encoding="utf-8").splitlines()[0]
        data = json.loads(first_line)
        return data.get("system", ""), data.get("messages", [])
    except Exception:  # noqa: BLE001
        return None, None


def _load_offline_mode() -> bool:
    try:
        from services.agent import config as _cfg  # type: ignore[import]
        return _cfg.offline_enabled()
    except Exception:  # noqa: BLE001
        return False


def _assert_offline_safe_provider(provider: Provider) -> None:
    try:
        from services.agent import config as _cfg  # type: ignore[import]
        _cfg.assert_offline_safe(provider)
    except Exception:  # noqa: BLE001
        # If config isn't importable (e.g. very early init), propagate any
        # OfflineModeError but swallow ImportError.
        name = getattr(provider, "name", "?")
        offline_safe = getattr(provider, "offline_safe", False)
        if not offline_safe and name not in ("mock", "mock-roles", "base"):
            raise RuntimeError(
                f"ShadowExecutor: challenger provider {name!r} may not be offline-safe "
                "and offline mode check failed — refusing to construct."
            ) from None


def _noop_record(record: ShadowRecord) -> None:
    pass
