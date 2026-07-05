"""services/agent/routing.py — Model routing table (aicad.routing/1).

Contract: contracts/advisor.md §14 (FROZEN).
Hunter directive 2026-07-04: routing.yaml is the ONLY place role→model bindings
live. No hardcoded role→model choices anywhere, no special-casing the overseer
in code. This module exposes ``resolve()`` + ``resolve_assignments()`` so any
caller (overseer, delegation engine, settings GUI via T-W17) can ask "what model
resolves for this role right now?"

Design:
  * Sources are registered by the user via the GUI settings (or programmatically
    for tests). Each source maps to an existing provider class in
    services/agent/providers/ — no new client layer.
  * Tier ranking (data, not code): frontier > strong > cheap_local. This is a
    static table used to interpret 'strongest'/'cheapest' preference tokens.
  * Availability probe: each source has an ``is_available()`` call (or a passed-in
    availability map for tests). A missing/offline source is skipped; the resolver
    falls through to the next preference.
  * Every resolution emits a ledger event ("sketcher → qwen3-4b@lmstudio because
    local-first preference") when a ledger is provided.
  * ``resolve_assignments()`` returns the per-role resolved models so the T-W17
    settings GUI can display live consequences.

Offline discipline: provider availability is checked via a pluggable probe so
tests can inject a mock without touching real network. No all-skip: tests verify
fallthrough, single-source collapse, and resolution events.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

ROUTING_SCHEMA = "aicad.routing/1"

# Tier ranking — higher value = more capable. Used to resolve 'strongest'/'cheapest'.
# This is DATA, not code: update the table here; roles.yaml and routing.yaml never
# embed a numeric tier.
_TIER_RANK: dict[str, int] = {
    "frontier": 3,
    "strong": 2,
    "cheap_local": 1,
}

# Legal preference tokens.
_PREFERENCE_TOKENS = frozenset({"strongest", "cheapest", "strong", "cheap_local"})

# Legal source kinds (maps to providers/ package classes).
_SOURCE_KINDS = frozenset({
    "anthropic", "openai", "openrouter", "gemini",
    "lmstudio", "llamacpp", "ollama", "vllm", "mock",
})

_ROUTING_YAML = Path(__file__).resolve().parent / "routing.yaml"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ModelEntry:
    """One model offered by a source."""
    id: str                  # e.g. "claude-sonnet-4-6"
    tier: str                # "frontier" | "strong" | "cheap_local"

    @property
    def tier_rank(self) -> int:
        return _TIER_RANK.get(self.tier, 0)


@dataclass
class Source:
    """One configured provider source."""
    id: str                  # e.g. "anthropic_api"
    kind: str                # e.g. "anthropic"
    models: list[ModelEntry] = field(default_factory=list)
    # Available at runtime — populated by resolve_assignments() or tests.
    _available: Optional[bool] = field(default=None, repr=False, compare=False)

    def best_model_at_tier(self, prefer_tier: str) -> Optional[ModelEntry]:
        """Return the best model matching *prefer_tier* exactly.
        Returns None if no model at that exact tier exists in this source.
        Use this for tier-preference resolution (strict match)."""
        exact = [m for m in self.models if m.tier == prefer_tier]
        if exact:
            return max(exact, key=lambda m: m.tier_rank)
        return None

    def best_model(self, prefer_tier: str) -> Optional[ModelEntry]:
        """Return the best model matching *prefer_tier* exactly, or the closest
        available tier if no exact match.  Returns None when models is empty.
        Used only for single-source collapse (no strict-match constraint)."""
        exact = self.best_model_at_tier(prefer_tier)
        if exact is not None:
            return exact
        # Closest: highest-ranked model that meets-or-exceeds the requested tier.
        rank_floor = _TIER_RANK.get(prefer_tier, 0)
        candidates = [m for m in self.models if m.tier_rank >= rank_floor]
        if candidates:
            return max(candidates, key=lambda m: m.tier_rank)
        # Fallback: any model (single-source degenerate case).
        if self.models:
            return max(self.models, key=lambda m: m.tier_rank)
        return None


@dataclass(frozen=True)
class Resolution:
    """Result of one routing resolution call."""
    role: str
    source_id: str
    model_id: str
    model_tier: str
    preference: str          # the winning token ("strongest", "cheap_local", …)
    fallthrough: bool = False  # True when an earlier preference was skipped offline


class RoutingError(ValueError):
    """Routing.yaml is malformed or an assignment cannot be resolved."""


# ---------------------------------------------------------------------------
# Parser / validator
# ---------------------------------------------------------------------------

def _parse_source(raw: dict, i: int) -> Source:
    if not isinstance(raw, dict):
        raise RoutingError(f"source[{i}]: must be a mapping, got {type(raw).__name__}")
    sid = raw.get("id")
    if not sid or not isinstance(sid, str):
        raise RoutingError(f"source[{i}]: missing or non-string 'id'")
    kind = raw.get("kind", "")
    if kind not in _SOURCE_KINDS:
        raise RoutingError(
            f"source '{sid}': kind {kind!r} not in {sorted(_SOURCE_KINDS)}"
        )
    models: list[ModelEntry] = []
    for j, mraw in enumerate(raw.get("models") or []):
        if not isinstance(mraw, dict):
            raise RoutingError(f"source '{sid}' model[{j}]: must be a mapping")
        mid = mraw.get("id")
        if not mid:
            raise RoutingError(f"source '{sid}' model[{j}]: missing 'id'")
        tier = mraw.get("tier", "cheap_local")
        if tier not in _TIER_RANK:
            raise RoutingError(
                f"source '{sid}' model '{mid}': tier {tier!r} not in {sorted(_TIER_RANK)}"
            )
        models.append(ModelEntry(id=str(mid), tier=tier))
    return Source(id=str(sid), kind=kind, models=models)


def _parse_preference_list(role: str, raw: Any) -> list[str]:
    """Parse a role's preference list.  Each entry is a token or 'explicit:src/model'."""
    if raw is None:
        return ["strongest"]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise RoutingError(f"assignment for {role!r}: must be a list, got {type(raw).__name__}")
    prefs: list[str] = []
    for p in raw:
        if not isinstance(p, str):
            raise RoutingError(f"assignment for {role!r}: preference {p!r} is not a string")
        if p in _PREFERENCE_TOKENS:
            prefs.append(p)
        elif re.match(r"^explicit:[^/]+/.+$", p):
            prefs.append(p)
        else:
            raise RoutingError(
                f"assignment for {role!r}: unknown preference token {p!r}. "
                f"Use one of {sorted(_PREFERENCE_TOKENS)} or 'explicit:<source_id>/<model_id>'"
            )
    return prefs or ["strongest"]


def load_routing(path: Path | None = None) -> "RoutingTable":
    """Parse and validate routing.yaml; return a RoutingTable.

    *path* defaults to the canonical file next to this module.  Accepts an
    in-memory dict for tests (pass a dict as *path*)."""
    if isinstance(path, dict):
        data = path
    else:
        p = path or _ROUTING_YAML
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise RoutingError(f"routing.yaml not found at {p}")
        data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise RoutingError("routing.yaml: top level must be a YAML mapping")
    schema = data.get("schema")
    if schema != ROUTING_SCHEMA:
        raise RoutingError(f"routing.yaml: schema must be {ROUTING_SCHEMA!r}, got {schema!r}")
    raw_sources = data.get("sources") or []
    sources: list[Source] = []
    for i, rs in enumerate(raw_sources):
        sources.append(_parse_source(rs, i))
    raw_assignments = data.get("assignments") or {}
    if not isinstance(raw_assignments, dict):
        raise RoutingError("routing.yaml: 'assignments' must be a mapping")
    assignments: dict[str, list[str]] = {}
    for role, pref_raw in raw_assignments.items():
        assignments[str(role)] = _parse_preference_list(str(role), pref_raw)
    raw_policy = data.get("approval_policy") or {}
    approval_policy = {
        "depth_1": str(raw_policy.get("depth_1", "auto")),
        "deeper": str(raw_policy.get("deeper", "overseer")),
    }
    return RoutingTable(sources=sources, assignments=assignments,
                        approval_policy=approval_policy)


# ---------------------------------------------------------------------------
# RoutingTable — the live resolver
# ---------------------------------------------------------------------------

class RoutingTable:
    """Parsed + validated routing table.  Thread-safe reads; no mutation after init."""

    def __init__(
        self,
        sources: list[Source],
        assignments: dict[str, list[str]],
        approval_policy: dict[str, str],
    ) -> None:
        self._sources: list[Source] = list(sources)
        self._by_id: dict[str, Source] = {s.id: s for s in sources}
        self._assignments: dict[str, list[str]] = dict(assignments)
        self.approval_policy: dict[str, str] = dict(approval_policy)

    # -- availability probe --------------------------------------------------

    def available_sources(
        self, availability: dict[str, bool] | None = None
    ) -> list[Source]:
        """Return sources whose availability is True.

        *availability* is a ``{source_id: bool}`` map injected by the caller (real
        liveness probe or test mock).  When a source is missing from the map it is
        assumed available (optimistic default — the caller should probe actively and
        pass the result).
        """
        out: list[Source] = []
        for src in self._sources:
            avail = (availability or {}).get(src.id, True)
            if avail:
                out.append(src)
        return out

    # -- core resolver -------------------------------------------------------

    def resolve(
        self,
        role: str,
        *,
        availability: dict[str, bool] | None = None,
        ledger: Any | None = None,
    ) -> Optional[Resolution]:
        """Resolve the model assignment for *role*.

        Returns a :class:`Resolution` or ``None`` when no source is available
        (the caller degrades gracefully — a missing routing result must not
        crash a mission, per §14 single-source collapse rule).

        Every successful resolution is appended to *ledger* as a routing event
        (actor='routing', kind='routing') for troubleshooting ("which model did that").
        """
        prefs = self._assignments.get(role) or self._assignments.get("default") or ["strongest"]
        avail = self.available_sources(availability)

        resolution = self._try_prefs(role, prefs, avail)
        if resolution is None and avail:
            # Single-source / degenerate collapse: all prefs failed but there is
            # a source — fall onto whatever it has.
            src = avail[0]
            m = src.best_model("cheap_local") or (src.models[0] if src.models else None)
            if m:
                resolution = Resolution(
                    role=role, source_id=src.id, model_id=m.id,
                    model_tier=m.tier, preference="collapse", fallthrough=True,
                )

        if resolution is not None and ledger is not None:
            _emit_routing_event(ledger, resolution)

        return resolution

    def _try_prefs(
        self, role: str, prefs: list[str], avail: list[Source]
    ) -> Optional[Resolution]:
        fallthrough = False
        for pref in prefs:
            r = self._try_one_pref(role, pref, avail, fallthrough=fallthrough)
            if r is not None:
                return r
            fallthrough = True  # subsequent preferences are fallthrough
        return None

    def _try_one_pref(
        self, role: str, pref: str, avail: list[Source], fallthrough: bool
    ) -> Optional[Resolution]:
        if pref == "strongest":
            return self._pick_by_tier(role, avail, highest=True, fallthrough=fallthrough)
        if pref == "cheapest":
            return self._pick_by_tier(role, avail, highest=False, fallthrough=fallthrough)
        if pref in ("strong", "cheap_local", "frontier"):
            return self._pick_by_preferred_tier(role, pref, avail, fallthrough=fallthrough)
        if pref.startswith("explicit:"):
            return self._pick_explicit(role, pref, avail, fallthrough=fallthrough)
        return None

    def _pick_by_tier(
        self, role: str, avail: list[Source], *, highest: bool, fallthrough: bool
    ) -> Optional[Resolution]:
        """Pick the highest or lowest tier model across all available sources."""
        candidates: list[tuple[int, Source, ModelEntry]] = []
        for src in avail:
            for m in src.models:
                candidates.append((_TIER_RANK.get(m.tier, 0), src, m))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0], reverse=highest)
        rank, src, m = candidates[0]
        return Resolution(
            role=role, source_id=src.id, model_id=m.id,
            model_tier=m.tier,
            preference="strongest" if highest else "cheapest",
            fallthrough=fallthrough,
        )

    def _pick_by_preferred_tier(
        self, role: str, tier: str, avail: list[Source], fallthrough: bool
    ) -> Optional[Resolution]:
        """Pick a model at exactly *tier* from available sources (strict match).

        Iterates all available sources looking for a model whose tier matches
        *tier* exactly.  Does not fall through to higher-tier models — that is
        the job of the caller's preference-list fallthrough.
        """
        for src in avail:
            m = src.best_model_at_tier(tier)
            if m:
                return Resolution(
                    role=role, source_id=src.id, model_id=m.id,
                    model_tier=m.tier, preference=tier, fallthrough=fallthrough,
                )
        return None

    def _pick_explicit(
        self, role: str, pref: str, avail: list[Source], fallthrough: bool
    ) -> Optional[Resolution]:
        """Handle 'explicit:<source_id>/<model_id>' preference token."""
        try:
            rest = pref[len("explicit:"):]
            src_id, model_id = rest.split("/", 1)
        except ValueError:
            return None
        src = self._by_id.get(src_id)
        if src is None:
            return None
        # Check source is available.
        if not any(s.id == src_id for s in avail):
            return None
        m_match = next((m for m in src.models if m.id == model_id), None)
        if m_match is None:
            return None
        return Resolution(
            role=role, source_id=src.id, model_id=m_match.id,
            model_tier=m_match.tier, preference=pref, fallthrough=fallthrough,
        )

    # -- resolve_assignments: settings GUI API (T-W17) -----------------------

    def resolve_assignments(
        self, *, availability: dict[str, bool] | None = None
    ) -> dict[str, Optional[Resolution]]:
        """Return the resolved model for every role in ``assignments``.

        This is the API the T-W17 settings GUI uses to display "what does
        'strongest' actually resolve to right now?" per Hunter's directive.
        Returns ``None`` for any role whose assignment cannot be resolved (e.g.
        no sources configured or all offline).
        """
        return {
            role: self.resolve(role, availability=availability)
            for role in self._assignments
        }

    # -- approval policy helpers ---------------------------------------------

    def approval_for_depth(self, depth: int, kind: str = "worker") -> str:
        """Return the approval policy for a delegation at *depth*.

        *kind* = 'ponder': auto-approved at ANY depth (read-only + budget-bounded).
        *depth* = 1 from the orchestrator: auto.
        *depth* > 1 and kind != 'ponder': overseer (or human, per the policy).
        """
        if kind == "ponder":
            return "auto"
        if depth <= 1:
            return self.approval_policy.get("depth_1", "auto")
        return self.approval_policy.get("deeper", "overseer")


# ---------------------------------------------------------------------------
# Ledger event emission
# ---------------------------------------------------------------------------

def _emit_routing_event(ledger: Any, res: Resolution) -> None:
    """Append a routing event to *ledger* — tolerant of any ledger-like object."""
    payload = {
        "schema": "aicad.routing/1",
        "id": f"rt_{uuid.uuid4().hex[:8]}",
        "role": res.role,
        "source_id": res.source_id,
        "model_id": res.model_id,
        "model_tier": res.model_tier,
        "preference": res.preference,
        "fallthrough": res.fallthrough,
    }
    try:
        ledger.append("routing", "routing", payload)
    except Exception:  # noqa: BLE001 — routing events must never crash a mission
        pass


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_default_table: Optional[RoutingTable] = None


def get_routing_table(path: Path | None = None, *, reload: bool = False) -> RoutingTable:
    """Load (and cache) the routing table from routing.yaml.

    Pass *reload=True* to force re-parse (e.g. after the GUI saves changes).
    Pass a dict as *path* to inject a test configuration.
    """
    global _default_table
    if reload or _default_table is None or isinstance(path, dict) or path is not None:
        _default_table = load_routing(path)
    return _default_table
