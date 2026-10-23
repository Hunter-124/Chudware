"""services/agent/routing.py — Model routing table (aicad.routing/1).

Contract: contracts/advisor.md §14 (FROZEN).
Design decision (2026-07-04): routing.yaml is the ONLY place role→model bindings
live. No hardcoded role→model choices anywhere, no special-casing the overseer
in code.

This module serves two integrated consumers:

1. **Delegation engine** — ``RoutingTable`` class:
   ``resolve()`` + ``resolve_assignments()`` so the overseer, delegation engine,
   and settings GUI can ask "what model resolves for this role right now?"
   Ledger events emitted on every resolution.

2. **C++ GUI settings panel** — module-level functions:
   ``load`` / ``loads`` / ``validate`` / ``resolve`` / ``warnings_for`` /
   ``view`` / ``dump`` / ``save`` + a ``get|set|validate`` CLI (JSON in/out).
   The C++ panels shell out to this CLI — it is the seam, not an import.

Both consumers share the same ``_TIER_RANK`` table and ``_SOURCE_KINDS``
constants. No parallel resolver — §14 lives here once.

Tier vocabulary (data, not code) — ``_TIER_RANK``:
  frontier > strong > local_large > cheap_local/local > cheap

  Canonical tokens (frontier, strong, cheap_local) are the delegation-engine
  preference vocabulary.  local_large / local / cheap are added for real-world
  model declarations; these rank consistently within the same table.

Pin-token formats:
  * RoutingTable (delegation engine): ``explicit:<source_id>/<model_id>``
  * Module-level API / CLI (GUI contract): ``<model_id>@<source_id>``
  Both forms are understood by their respective consumers; neither bleeds into
  the other.

Offline discipline: provider availability is pluggable so tests inject a mock
without touching real network. No all-skip: tests verify fallthrough,
single-source collapse, and resolution events.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

ROUTING_SCHEMA = "aicad.routing/1"
SCHEMA = ROUTING_SCHEMA  # alias for module-level / GUI consumers

# Tier ranking — higher value = more capable.
# Canonical tokens (frontier, strong, cheap_local) are the delegation-engine
# preference vocabulary. local_large / local / cheap are added for source
# declarations; they rank within the same table for consistent resolution.
_TIER_RANK: dict[str, int] = {
    "frontier":    40,  # large cloud flagship
    "strong":      30,  # mid-tier cloud or >=70 B local
    "local_large": 20,  # large local (>=30 B)
    "cheap_local": 15,  # small/fast local (canonical preference token)
    "local":       10,  # small local (source declaration alias)
    "cheap":        5,  # smallest / cheapest
}

# Legal preference tokens for the delegation-engine preference lists.
_PREFERENCE_TOKENS = frozenset({"strongest", "cheapest", "strong", "cheap_local"})

# Legal source kinds — NO consumer-OAuth kinds (Pillar 12).
_SOURCE_KINDS = frozenset({
    "anthropic", "openai", "openrouter", "gemini",
    "lmstudio", "llamacpp", "ollama", "vllm", "mock",
})

# Kinds that run on the user's own machine (for cheap_local/local resolution
# and quality-gate warnings).
_LOCAL_KINDS = frozenset({"lmstudio", "llamacpp", "ollama", "vllm"})

# Module-level API: symbolic tokens understood by resolve() / validate().
SYMBOLIC_TOKENS = ("strongest", "strong", "cheap_local", "cheapest")

# Public view of the tier vocabulary _parse_source()/validate() accept for a model's
# "tier" field. Sources written to routing.yaml must use one of these tiers; the
# context-length buckets in services.agent.tiers (local-24k/local-48k/mid) are
# different, and load_routing() refuses unknown tiers.
KNOWN_TIERS = frozenset(_TIER_RANK)

# Source kinds exposed by the GUI (subset of _SOURCE_KINDS, no internal mocks).
ALLOWED_SOURCE_KINDS = (
    "anthropic", "openai", "openrouter",
    "lmstudio", "llamacpp", "ollama",
)

# Roles whose model choice gates mission quality; weak local picks earn a
# non-blocking warning. Never hardcodes a model — only which roles to caution.
_QUALITY_CRITICAL_ROLES = ("overseer", "orchestrator", "reviewer", "fixer")

_ROUTING_YAML = Path(__file__).resolve().parent / "routing.yaml"
DEFAULT_PATH = _ROUTING_YAML  # alias used by module-level load/save


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ModelEntry:
    """One model offered by a source."""
    id: str    # e.g. "claude-sonnet-4-6"
    tier: str  # e.g. "frontier" | "strong" | "cheap_local" | "local"

    @property
    def tier_rank(self) -> int:
        return _TIER_RANK.get(self.tier, 0)


@dataclass
class Source:
    """One configured provider source."""
    id: str                                       # e.g. "anthropic_api"
    kind: str                                     # e.g. "anthropic"
    models: list[ModelEntry] = field(default_factory=list)
    base_url: Optional[str] = None                # e.g. "http://127.0.0.1:8765/v1" (local kinds)
    _available: Optional[bool] = field(default=None, repr=False, compare=False)

    def best_model_at_tier(self, prefer_tier: str) -> Optional[ModelEntry]:
        """Return the best model at *prefer_tier* exactly (strict match)."""
        exact = [m for m in self.models if m.tier == prefer_tier]
        if exact:
            return max(exact, key=lambda m: m.tier_rank)
        return None

    def best_model(self, prefer_tier: str) -> Optional[ModelEntry]:
        """Best model at *prefer_tier*, or closest available tier."""
        exact = self.best_model_at_tier(prefer_tier)
        if exact is not None:
            return exact
        rank_floor = _TIER_RANK.get(prefer_tier, 0)
        candidates = [m for m in self.models if m.tier_rank >= rank_floor]
        if candidates:
            return max(candidates, key=lambda m: m.tier_rank)
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
    preference: str    # winning token ("strongest", "cheap_local", ...)
    fallthrough: bool = False  # True when an earlier preference was skipped


class RoutingError(ValueError):
    """Routing.yaml is malformed or an assignment cannot be resolved."""


# ---------------------------------------------------------------------------
# Parser / validator  (delegation-engine / RoutingTable path)
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
    base_url = raw.get("base_url")
    return Source(id=str(sid), kind=kind, models=models,
                  base_url=str(base_url) if base_url else None)


def _parse_preference_list(role: str, raw: Any) -> list[str]:
    """Parse a role's preference list for the delegation engine.

    Each entry is a symbolic token or ``explicit:<source_id>/<model_id>``
    (delegation-engine pin format).
    """
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

    *path* defaults to the canonical file next to this module.
    Pass a dict as *path* to inject a test configuration.
    """
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
    # Parse the delegation: block for soft rails config.
    raw_delegation = data.get("delegation") or {}
    delegation_config: dict[str, int] = {
        "soft_depth_limit": int(raw_delegation.get("soft_depth_limit", 8)),
        "max_agents": int(raw_delegation.get("max_agents", 64)),
    }
    return RoutingTable(sources=sources, assignments=assignments,
                        delegation_config=delegation_config)


# ---------------------------------------------------------------------------
# RoutingTable — delegation engine resolver (authoritative)
# ---------------------------------------------------------------------------

class RoutingTable:
    """Parsed + validated routing table.  Thread-safe reads; no mutation after init."""

    def __init__(
        self,
        sources: list[Source],
        assignments: dict[str, list[str]],
        delegation_config: dict[str, int] | None = None,
    ) -> None:
        self._sources: list[Source] = list(sources)
        self._by_id: dict[str, Source] = {s.id: s for s in sources}
        self._assignments: dict[str, list[str]] = dict(assignments)
        # Soft rails from routing.yaml delegation: block.
        _dc = delegation_config or {}
        self.soft_depth_limit: int = int(_dc.get("soft_depth_limit", 8))
        self.max_agents: int = int(_dc.get("max_agents", 64))

    # -- source lookup --------------------------------------------------------

    def get_source(self, source_id: str) -> Optional[Source]:
        """Return the configured :class:`Source` for *source_id*, or ``None``.

        Used by consumers of :meth:`resolve` (e.g. ``services.agent.config``) that
        need more than the id — the source's ``kind``/``base_url`` — to actually
        construct a provider from a routing decision.
        """
        return self._by_id.get(source_id)

    # -- availability probe --------------------------------------------------

    def available_sources(
        self, availability: dict[str, bool] | None = None
    ) -> list[Source]:
        """Return sources whose availability is True.

        *availability* is a ``{source_id: bool}`` map injected by the caller.
        Sources missing from the map are assumed available (optimistic default).
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

        Returns a :class:`Resolution` or ``None`` when no source is available.
        Every successful resolution is appended to *ledger* as a routing event.
        """
        prefs = self._assignments.get(role) or self._assignments.get("default") or ["strongest"]
        avail = self.available_sources(availability)

        resolution = self._try_prefs(role, prefs, avail)
        if resolution is None and avail:
            # Single-source / degenerate collapse.
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
            fallthrough = True
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
        """Pick a model at exactly *tier* from available sources (strict match)."""
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
        """Handle ``explicit:<source_id>/<model_id>`` preference token."""
        try:
            rest = pref[len("explicit:"):]
            src_id, model_id = rest.split("/", 1)
        except ValueError:
            return None
        src = self._by_id.get(src_id)
        if src is None:
            return None
        if not any(s.id == src_id for s in avail):
            return None
        m_match = next((m for m in src.models if m.id == model_id), None)
        if m_match is None:
            return None
        return Resolution(
            role=role, source_id=src.id, model_id=m_match.id,
            model_tier=m_match.tier, preference=pref, fallthrough=fallthrough,
        )

    # -- resolve_assignments: settings GUI API -------------------------------

    def resolve_assignments(
        self, *, availability: dict[str, bool] | None = None
    ) -> dict[str, Optional[Resolution]]:
        """Return the resolved model for every role in ``assignments``.

        Used by the settings GUI to display live resolution consequences.
        Returns ``None`` for any role that cannot be resolved.
        """
        return {
            role: self.resolve(role, availability=availability)
            for role in self._assignments
        }



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
# Module-level convenience (RoutingTable cache)
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


# ---------------------------------------------------------------------------
# Module-level API  (GUI / C++ CLI contract)
#
# These functions operate on raw dicts (the YAML document as parsed) rather
# than RoutingTable instances.  They are the boundary the C++ GUI shells
# across via subprocess.  Pin format here: ``<model_id>@<source_id>``.
# ---------------------------------------------------------------------------

def validate(data: dict[str, Any]) -> None:
    """Structural + semantic validation of a routing document.

    Raises :class:`RoutingError` on any issue.  Rules (section 14):
      * schema tag present and correct
      * sources: list of {id, kind in ALLOWED_SOURCE_KINDS, models:[{id, tier}]};
        unique ids; each source has at least one model
      * assignments: role->non-empty preference list; each token is a known
        symbolic token OR a ``<model_id>@<source_id>`` pin whose source exists
      * a ``default`` assignment is required (fallback for unlisted roles)
    """
    if data.get("schema") != SCHEMA:
        raise RoutingError(f"schema must be '{SCHEMA}', got {data.get('schema')!r}")

    sources = data.get("sources")
    if not isinstance(sources, list):
        raise RoutingError("'sources' must be a list")
    seen_ids: set[str] = set()
    source_ids: set[str] = set()
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            raise RoutingError(f"sources[{i}] must be a mapping")
        sid = src.get("id")
        if not isinstance(sid, str) or not sid:
            raise RoutingError(f"sources[{i}] missing non-empty 'id'")
        if sid in seen_ids:
            raise RoutingError(f"duplicate source id '{sid}'")
        seen_ids.add(sid)
        source_ids.add(sid)
        kind = src.get("kind")
        if kind not in ALLOWED_SOURCE_KINDS:
            raise RoutingError(
                f"source '{sid}' kind '{kind}' not allowed; "
                f"pick one of {', '.join(ALLOWED_SOURCE_KINDS)}"
            )
        models = src.get("models")
        if not isinstance(models, list) or not models:
            raise RoutingError(f"source '{sid}' must list at least one model")
        for j, m in enumerate(models):
            if not isinstance(m, dict) or not m.get("id"):
                raise RoutingError(f"source '{sid}' models[{j}] missing 'id'")
            tier = m.get("tier")
            if tier is not None and tier not in _TIER_RANK:
                raise RoutingError(
                    f"source '{sid}' model '{m.get('id')}' tier '{tier}' unknown; "
                    f"use one of {', '.join(sorted(_TIER_RANK, key=lambda k: -_TIER_RANK[k]))}"
                )

    assignments = data.get("assignments")
    if not isinstance(assignments, dict) or not assignments:
        raise RoutingError("'assignments' must be a non-empty mapping of role->prefs")
    if "default" not in assignments:
        raise RoutingError("'assignments' must include a 'default' fallback row")
    for role, prefs in assignments.items():
        if not isinstance(prefs, list) or not prefs:
            raise RoutingError(f"assignment for role '{role}' must be a non-empty list")
        for tok in prefs:
            if not isinstance(tok, str) or not tok:
                raise RoutingError(f"role '{role}' has an empty preference token")
            if tok in SYMBOLIC_TOKENS:
                continue
            # concrete pin "<model_id>@<source_id>"
            if "@" in tok:
                _, _, pin_src = tok.partition("@")
                if pin_src not in source_ids:
                    raise RoutingError(
                        f"role '{role}' pins source '{pin_src}' which is not configured"
                    )
                continue
            raise RoutingError(
                f"role '{role}' token '{tok}' is neither a symbolic tier "
                f"({', '.join(SYMBOLIC_TOKENS)}) nor a '<model_id>@<source_id>' pin"
            )



def loads(text: str) -> dict[str, Any]:
    """Parse YAML *text* and validate it. Returns the normalized routing doc."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RoutingError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RoutingError("routing document must be a mapping")
    validate(data)
    return data


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Read + validate the routing table at *path* (default: routing.yaml)."""
    p = Path(path) if path is not None else DEFAULT_PATH
    if not p.exists():
        raise RoutingError(f"routing file not found: {p}")
    return loads(p.read_text(encoding="utf-8"))


def from_obj(obj: Any) -> dict[str, Any]:
    """Validate an in-memory routing model (e.g. decoded from GUI JSON)."""
    if not isinstance(obj, dict):
        raise RoutingError("routing model must be an object")
    validate(obj)
    return obj


# ---------------------------------------------------------------------------
# Module-level resolution (uses _TIER_RANK directly; no RoutingTable needed)
# ---------------------------------------------------------------------------

def _iter_models(data: dict[str, Any]):
    """Yield (source_id, kind, model_id, tier, rank) over all configured models."""
    for src in data.get("sources", []):
        sid = src.get("id", "")
        kind = src.get("kind", "")
        for m in src.get("models", []):
            tier = m.get("tier")
            rank = _TIER_RANK.get(tier, 0)
            yield sid, kind, m.get("id", ""), tier, rank


def _resolve_token(data: dict[str, Any], token: str):
    """Resolve one preference token to (model_id, source_id) or None if unmet."""
    models = list(_iter_models(data))
    if not models:
        return None

    # concrete pin "<model_id>@<source_id>"
    if "@" in token and token not in SYMBOLIC_TOKENS:
        model_id, _, source_id = token.partition("@")
        for sid, _kind, mid, _tier, _rank in models:
            if sid == source_id and mid == model_id:
                return (mid, sid)
        return None

    if token in ("strongest", "strong"):
        best = max(models, key=lambda t: t[4])
        return (best[2], best[0]) if best[4] > 0 or best[2] else None

    if token in ("cheapest", "cheap_local"):
        if token == "cheap_local":
            local = [m for m in models if m[1] in _LOCAL_KINDS]
            pool = local or []
        else:
            pool = list(models)
        if not pool:
            return None
        cheapest = min(pool, key=lambda t: t[4] if t[4] > 0 else 99)
        return (cheapest[2], cheapest[0])

    return None


def resolve(data: dict[str, Any], role: str):
    """Return (model_id, source_id) the role resolves to, or None.

    Walks the role's preference list (falling back to ``default``), returning
    the first token that resolves against the current sources.
    """
    assignments = data.get("assignments", {})
    prefs = assignments.get(role) or assignments.get("default") or []
    for token in prefs:
        hit = _resolve_token(data, token)
        if hit is not None:
            return hit
    return None


def warnings_for(data: dict[str, Any], role: str) -> list[str]:
    """Non-blocking advisories for a role's current resolution. Warn, never block."""
    out: list[str] = []
    hit = resolve(data, role)
    if hit is None:
        out.append("no configured source satisfies this preference — role would fall back")
        return out
    model_id, source_id = hit
    for sid, kind, mid, _tier, rank in _iter_models(data):
        if sid == source_id and mid == model_id:
            if (role in _QUALITY_CRITICAL_ROLES
                    and kind in _LOCAL_KINDS
                    and rank <= _TIER_RANK["local"]):
                out.append(
                    f"'{role}' resolves to a small local model ({model_id}); "
                    "quality here gates the whole mission — consider a stronger source"
                )
            break
    return out


# ---------------------------------------------------------------------------
# View (GUI + delegation-engine handoff contract)
# ---------------------------------------------------------------------------

def view(data: dict[str, Any]) -> dict[str, Any]:
    """The JSON the GUI renders and the delegation driver consumes.

    Shape::

        {
          "schema": "aicad.routing/1",
          "sources": [...],
          "assignments": {role: [prefs]},
          "resolved": {role: {"model": "...", "source": "...", "warnings": [...]}},
        }

    ``resolved`` shows the concrete consequence of each preference list given
    the current sources, so the user sees the effect of every edit.
    """
    resolved: dict[str, Any] = {}
    for role in data.get("assignments", {}):
        hit = resolve(data, role)
        resolved[role] = {
            "model": hit[0] if hit else None,
            "source": hit[1] if hit else None,
            "warnings": warnings_for(data, role),
        }
    return {
        "schema": SCHEMA,
        "sources": data.get("sources", []),
        "assignments": data.get("assignments", {}),
        "resolved": resolved,
    }


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------

def dump(data: dict[str, Any]) -> str:
    """Validate then serialize to YAML text (does not touch disk)."""
    validate(data)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def save(data: dict[str, Any], path: str | Path | None = None) -> Path:
    """Validate + write *data* to *path* (default: routing.yaml). Returns path."""
    p = Path(path) if path is not None else DEFAULT_PATH
    text = dump(data)  # validates first — a bad model never reaches disk
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# CLI  (the seam the C++ GUI panels shell out to)
#
# Usage:
#   python3 -m services.agent.routing get [--path PATH]
#   python3 -m services.agent.routing set [--path PATH]   # JSON on stdin
#   python3 -m services.agent.routing validate [--path PATH]
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python3 -m services.agent.routing")
    ap.add_argument("cmd", choices=("get", "set", "validate"))
    ap.add_argument("--path", default=None, help="routing.yaml path (default: shipped)")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "get":
            data = load(args.path)
            print(json.dumps(view(data)))
            return 0
        if args.cmd == "validate":
            load(args.path)
            print(json.dumps({"ok": True}))
            return 0
        # set: read a routing model as JSON on stdin, validate, write, echo view.
        raw = sys.stdin.read()
        obj = json.loads(raw)
        # The GUI may send either a full routing doc or a subset; normalize.
        doc = {
            "schema": SCHEMA,
            "sources": obj.get("sources", []),
            "assignments": obj.get("assignments", {}),
        }
        validate(doc)
        save(doc, args.path)
        print(json.dumps(view(doc)))
        return 0
    except RoutingError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {exc}"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
