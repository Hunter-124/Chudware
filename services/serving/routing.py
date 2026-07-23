"""services/serving/routing.py — per-insertion-point adapter routing (T-H9).

Fills the ``routing`` seam reserved in ``contracts/providers.md`` §7. The routing table
maps each agent insertion point (``IP-N``) to a :class:`RouteEntry`::

    {insertion_point -> RouteEntry(provider, model, adapter_version, fallback, canary)}

and :meth:`RoutingTable.resolve` turns an entry + the caller's identity into a concrete
:class:`RouteDecision` (which provider/model/adapter to actually call, and whether the
response should be telemetry-tagged as a canary).

Kill-switch invariant (contract §7, Erik-ratified): ``fallback`` is ALWAYS a
deterministic-path or frontier route — never another experimental adapter. It doubles as
(a) the incumbent for canary-excluded traffic and (b) the degrade target when the adapter
is unavailable. A :class:`Fallback` structurally cannot carry an ``adapter_version``, so
"fall back onto another LoRA" is unrepresentable by construction.

Rollout ladder (docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md §5.7-5.8):
    shadow (T-H7)  ->  canary (canary_pct % + per-user opt-in)  ->  promote (100%, gated).

Rollback = flip ``adapter_version`` back to a previous DVC-versioned adapter (see
:mod:`services.serving.promotion`). Nothing here overwrites an adapter; only routing flips.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# Kill-switch classes for a fallback route. A fallback is NEVER an experimental adapter.
FALLBACK_MODES = ("deterministic", "frontier")


class RoutingError(ValueError):
    """Raised when a routing table / entry violates a frozen invariant."""


# ---------------------------------------------------------------------------
# Fallback (kill-switch target)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fallback:
    """The safe degrade target for an insertion point (kill-switch rule, contract §7).

    ``mode`` must be one of :data:`FALLBACK_MODES`:

    - ``"deterministic"`` — the non-model deterministic path (no provider call at all);
      ``provider``/``model`` are advisory labels only.
    - ``"frontier"``      — a hosted frontier provider (e.g. ``anthropic``) as the safety net.

    A Fallback has NO ``adapter_version`` field on purpose: an experimental LoRA can never
    be a fallback. This is the structural enforcement of the kill-switch invariant.
    """

    mode: str = "frontier"
    provider: str = "anthropic"
    model: str = ""

    def __post_init__(self) -> None:
        if self.mode not in FALLBACK_MODES:
            raise RoutingError(
                f"fallback.mode must be one of {FALLBACK_MODES} (kill-switch rule); "
                f"got {self.mode!r}. A fallback may never be an experimental adapter."
            )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fallback":
        return cls(
            mode=str(d.get("mode", "frontier")),
            provider=str(d.get("provider", "anthropic")),
            model=str(d.get("model", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "provider": self.provider, "model": self.model}


# ---------------------------------------------------------------------------
# RouteEntry (one insertion point's config)
# ---------------------------------------------------------------------------

@dataclass
class RouteEntry:
    """Routing config for a single insertion point.

    Attributes
    ----------
    insertion_point:
        The IP tag, e.g. ``"IP-13"``.
    provider:
        The provider that serves the adapter, e.g. ``"vllm"`` (an OpenAI-compatible
        vLLM instance) or ``"llamacpp"`` for a merged-GGUF local serve.
    model:
        The base model id served at this endpoint.
    adapter_version:
        The DVC-versioned LoRA adapter identifier (e.g. ``"IP-13/v3"``), or ``None`` to
        serve the base model with no LoRA. ``None`` means "route is on the base/fallback".
    fallback:
        The kill-switch :class:`Fallback` (deterministic or frontier). REQUIRED and always
        safe.
    canary_pct:
        Percentage (0-100) of *non-opted-in* traffic routed to the adapter. ``0`` = adapter
        served to opt-in users only (or nobody); ``100`` = fully promoted (adapter is the
        incumbent, fallback used only on failure).
    canary_opt_in:
        Explicit user/session ids ("early adopters") always routed to the adapter regardless
        of ``canary_pct``.
    canary_salt:
        Stable salt for deterministic bucketing (so a user is stably in/out across flips and
        tests are reproducible). Defaults to the insertion point.
    history:
        Previously-served ``adapter_version`` values, most-recent-last. :func:`promotion.promote`
        pushes the outgoing version here; :func:`promotion.rollback` pops from it.
    """

    insertion_point: str
    provider: str
    model: str
    adapter_version: str | None = None
    fallback: Fallback = field(default_factory=Fallback)
    canary_pct: float = 0.0
    canary_opt_in: tuple[str, ...] = ()
    canary_salt: str = ""
    history: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.insertion_point:
            raise RoutingError("RouteEntry.insertion_point must be non-empty")
        if not isinstance(self.fallback, Fallback):
            raise RoutingError("RouteEntry.fallback must be a Fallback (kill-switch rule)")
        if not (0.0 <= float(self.canary_pct) <= 100.0):
            raise RoutingError(
                f"canary_pct must be in [0,100]; got {self.canary_pct!r}"
            )
        self.canary_pct = float(self.canary_pct)
        self.canary_opt_in = tuple(self.canary_opt_in)
        self.history = tuple(self.history)
        if not self.canary_salt:
            self.canary_salt = self.insertion_point

    @classmethod
    def from_dict(cls, insertion_point: str, d: dict[str, Any]) -> "RouteEntry":
        canary = d.get("canary", {}) or {}
        fb = d.get("fallback")
        if not fb:
            raise RoutingError(
                f"{insertion_point}: a 'fallback' is REQUIRED (kill-switch rule, contract §7)"
            )
        return cls(
            insertion_point=insertion_point,
            provider=str(d.get("provider", "")),
            model=str(d.get("model", "")),
            adapter_version=(d.get("adapter_version") or None),
            fallback=Fallback.from_dict(fb),
            canary_pct=float(canary.get("pct", 0.0)),
            canary_opt_in=tuple(canary.get("opt_in", []) or []),
            canary_salt=str(canary.get("salt", "") or ""),
            history=tuple(d.get("history", []) or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "adapter_version": self.adapter_version,
            "fallback": self.fallback.to_dict(),
            "canary": {
                "pct": self.canary_pct,
                "opt_in": list(self.canary_opt_in),
                "salt": self.canary_salt,
            },
            "history": list(self.history),
        }


# ---------------------------------------------------------------------------
# RouteDecision (the resolved answer)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteDecision:
    """The concrete routing result for one call.

    ``adapter`` is the served adapter version (``None`` when the base model or the fallback
    answered). ``is_canary`` is True only when the adapter served *during a partial rollout*
    (canary_pct < 100 or via opt-in) — a fully-promoted adapter is production, not canary.
    """

    insertion_point: str
    provider: str
    model: str
    adapter: str | None
    is_fallback: bool
    is_canary: bool
    reason: str

    def telemetry_tag(self) -> dict[str, Any]:
        """The ``model`` block to splice into an ``ai_artifact`` event (contract T-H1).

        This is the single decided write-point for DoD "every canary response tagged": the
        selected adapter is threaded from the routing decision into ``model.adapter`` (the
        LoggingProvider proxy does not expose ``.adapter`` and hardcodes ``None``).
        """
        return {"provider": self.provider, "model": self.model, "adapter": self.adapter}


# ---------------------------------------------------------------------------
# Deterministic canary bucketing
# ---------------------------------------------------------------------------

def canary_bucket(insertion_point: str, user_id: str, salt: str) -> int:
    """Return a stable bucket in ``[0, 100)`` for (insertion_point, user_id, salt).

    Deterministic (SHA-256 of the tuple) so a given user is stably in/out of a canary across
    routing-table flips, and the traffic percentage is exactly reproducible in tests. Flipping
    ``canary_pct`` 0->10->0 changes *who is admitted*, never the underlying bucket assignment.
    """
    key = f"{salt}\x00{insertion_point}\x00{user_id}".encode("utf-8")
    return int(hashlib.sha256(key).hexdigest(), 16) % 100


# ---------------------------------------------------------------------------
# RoutingTable
# ---------------------------------------------------------------------------

class RoutingTable:
    """A collection of :class:`RouteEntry`, keyed by insertion point."""

    def __init__(self, entries: Iterable[RouteEntry] | None = None) -> None:
        self._entries: dict[str, RouteEntry] = {}
        for e in entries or []:
            self._entries[e.insertion_point] = e

    # -- access ---------------------------------------------------------------

    def __contains__(self, insertion_point: str) -> bool:
        return insertion_point in self._entries

    def get(self, insertion_point: str) -> RouteEntry | None:
        return self._entries.get(insertion_point)

    def entries(self) -> dict[str, RouteEntry]:
        return dict(self._entries)

    def put(self, entry: RouteEntry) -> None:
        self._entries[entry.insertion_point] = entry

    # -- resolution -----------------------------------------------------------

    def resolve(
        self,
        insertion_point: str,
        *,
        user_id: str = "",
        adapter_healthy: bool = True,
    ) -> RouteDecision:
        """Resolve which brain answers a call at *insertion_point* for *user_id*.

        Order (all safe):
        1. Unknown insertion point  -> RoutingError (caller decides its own default).
        2. No adapter configured OR ``adapter_healthy`` False -> the fallback (kill-switch).
        3. Caller opted in           -> the adapter (canary).
        4. Deterministic bucket < canary_pct -> the adapter (canary if pct<100, else prod).
        5. Otherwise                 -> the fallback (the incumbent for canary-excluded traffic).

        ``adapter_healthy=False`` is how a live serving-failure degrades WITHOUT a user-facing
        error — the caller catches the adapter exception and re-resolves with False.
        """
        entry = self._entries.get(insertion_point)
        if entry is None:
            raise RoutingError(f"no route for insertion point {insertion_point!r}")

        # (2) adapter unavailable / not configured -> deterministic-or-frontier fallback.
        if not entry.adapter_version or not adapter_healthy:
            reason = "no_adapter" if not entry.adapter_version else "adapter_unhealthy"
            return self._fallback_decision(entry, reason)

        opted_in = user_id in entry.canary_opt_in
        if opted_in:
            selected, why = True, "opt_in"
        else:
            bucket = canary_bucket(insertion_point, user_id, entry.canary_salt)
            selected = bucket < entry.canary_pct
            why = "bucket_in" if selected else "bucket_out"

        if not selected:
            # canary-excluded traffic runs the incumbent == the safe fallback.
            return self._fallback_decision(entry, f"canary:{why}")

        # adapter serves. It is a *canary* response unless fully promoted (100% and not opt-in-only).
        is_canary = opted_in or entry.canary_pct < 100.0
        return RouteDecision(
            insertion_point=insertion_point,
            provider=entry.provider,
            model=entry.model,
            adapter=entry.adapter_version,
            is_fallback=False,
            is_canary=is_canary,
            reason=f"adapter:{why}",
        )

    def _fallback_decision(self, entry: RouteEntry, reason: str) -> RouteDecision:
        fb = entry.fallback
        return RouteDecision(
            insertion_point=entry.insertion_point,
            provider=fb.provider,
            model=fb.model,
            adapter=None,
            is_fallback=True,
            is_canary=False,
            reason=f"fallback:{reason}:{fb.mode}",
        )

    # -- serialization --------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoutingTable":
        entries = [RouteEntry.from_dict(ip, cfg) for ip, cfg in (d or {}).items()]
        return cls(entries)

    def to_dict(self) -> dict[str, Any]:
        return {ip: e.to_dict() for ip, e in self._entries.items()}


# ---------------------------------------------------------------------------
# Config persistence (config.json "routing" subtree)
# ---------------------------------------------------------------------------
# The routing table lives under the "routing" key of the SAME config.json that holds
# provider/model/offline settings, so a rollback or canary flip is a single-file edit
# (<1 min, no restart, nothing overwritten). We merge the subtree and never clobber the
# rest of the config.

ROUTING_KEY = "routing"


def _config_path():
    from services import paths  # lazy: keep import cost off module load
    return paths.CONFIG


def load_routing_table(cfg: dict | None = None) -> RoutingTable:
    """Load the routing table from *cfg* (or config.json's ``routing`` subtree)."""
    if cfg is None:
        import json
        p = _config_path()
        try:
            cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:  # noqa: BLE001
            cfg = {}
    return RoutingTable.from_dict((cfg or {}).get(ROUTING_KEY, {}) or {})


def save_routing_table(table: RoutingTable, cfg_path=None) -> None:
    """Persist *table* into config.json's ``routing`` subtree, preserving all other keys."""
    import json
    p = cfg_path or _config_path()
    try:
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        existing = {}
    existing[ROUTING_KEY] = table.to_dict()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def with_canary_pct(entry: RouteEntry, pct: float) -> RouteEntry:
    """Return a copy of *entry* with a new canary percentage (live flip helper)."""
    if not (0.0 <= float(pct) <= 100.0):
        raise RoutingError(f"canary pct must be in [0,100]; got {pct!r}")
    return replace(entry, canary_pct=float(pct))
