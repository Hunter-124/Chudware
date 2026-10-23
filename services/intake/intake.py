"""The "intake brain" — pure, stdlib-only logic for requirements intake, photo measurement,
and first-try fit, per ``docs/INTAKE_MEASUREMENT_DFM_PLAN.md`` (pruned 2026-07-23; see
``services/intake/__init__.py`` for the surviving design summary).

This module owns every deterministic decision the plan describes so that the MCP tools and the
in-app agent stage are *thin adapters* over one shared core (the plan's parity contract:
identical schema + gate + scorer + process-math on every path). Nothing here touches the engine,
OCP, the network, or the filesystem — every public function is a transformation over plain dicts.

The seven capability blocks (matching the task spec and the plan sections):

1. **Schemas (§2)** — :class:`MeasuredPartProfile`, :class:`MeasuredFeature`,
   :class:`CriticalDimension`, :class:`CriticalDimensionLedger`, :class:`FitReport`,
   :class:`IntakeState`. Each is a dataclass with a ``new_*`` factory and JSON-round-trippable
   ``to_dict`` / ``from_dict`` so it persists byte-for-byte into the ``.step+`` doc like ``studies``.
2. **Mating-feature taxonomy** — the :data:`ARCHETYPES` table (critical_keys + default fit_class,
   all ``is_critical=True``) and :func:`classify` mapping plain words to an archetype.
3. **Critical-dim ledger (§3 gate)** — :func:`build_ledger`, :func:`unresolved_critical`,
   :func:`apply_confirmation`, :func:`gate_ok`, :func:`assumptions`.
4. **Confidence scorer + demand rule (§5, the heart)** — :func:`score_claim`,
   :func:`required_tier`, :func:`is_satisfied`, :func:`compute_demands`.
5. **Photo-request builder (§1 step 5)** — :func:`build_photo_request`.
6. **FitReport + coupon (§1 step 10, §6 gate 3)** — :func:`build_fit_report`, :func:`coupon_spec`.
7. **Never-block policy (§3)** — :func:`gate_decision`.

It depends on the DfM "fit brain" for the clearance/fit math (``modeled_dim``,
``get_profile``, the known-process list). That brain now lives in the C++ core
(``core/dfm/process_profiles.cpp``, dispatched via the libchudware ``dfm_*`` methods), so this
module reaches it through ``core_bridge`` — the fit numbers stay identical to the planner's.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from services.mcp import core_bridge as _cb


class _ProfileView:
    """Read-only view over one ``dfm_get_profile`` result, exposing the
    ``fit_classes_mm`` attribute the oracle's ``ProcessProfile`` carried."""

    def __init__(self, profile: dict[str, Any]) -> None:
        self._params = profile.get("params") or {}

    @property
    def fit_classes_mm(self) -> dict[str, Any]:
        return self._params.get("fit_classes_mm") or {}


class _ProfilesMap:
    """Lazy, cache-backed stand-in for the oracle's ``PROFILES`` dict (id -> view).

    Unknown processes behave exactly like a missing dict key (``get`` -> None,
    ``in`` -> False) — ``dfm_get_profile`` reports them as ``ok: false``.
    """

    def __init__(self) -> None:
        self._cache: dict[str, _ProfileView] = {}

    def __getitem__(self, proc: str) -> _ProfileView:
        view = self.get(proc)
        if view is None:
            raise KeyError(proc)
        return view

    def __contains__(self, proc: Any) -> bool:
        return self.get(proc) is not None

    def get(self, proc: str, default: Any = None) -> Any:
        if proc not in self._cache:
            r = _cb.call("dfm_get_profile", {"process": proc})
            if not r.get("ok"):
                return default
            self._cache[proc] = _ProfileView(r.get("result") or {})
        return self._cache[proc]


class _ProcessProfilesBridge:
    """core_bridge-backed stand-in for the deleted ``engine.dfm.process_profiles`` module.

    Only the surface this module consumes is replicated: ``DEFAULT_PROCESS``, ``PROFILES``
    (id -> ``.fit_classes_mm`` view), ``modeled_dim`` and ``fit_clearance``. The oracle's
    ``ValueError`` on a bad input is preserved by raising on an ``ok: false`` envelope.
    """

    DEFAULT_PROCESS = "fdm"

    def __init__(self) -> None:
        self.PROFILES = _ProfilesMap()

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        r = _cb.call(method, params)
        if not r.get("ok"):
            raise ValueError(r.get("error") or f"{method} failed")
        return r.get("result")

    def modeled_dim(self, nominal_mm: float, fit_class: str, process: str,
                    is_hole: bool = False) -> dict[str, Any]:
        return self._call("dfm_modeled_dim", {
            "nominal_mm": float(nominal_mm), "fit_class": fit_class,
            "process": process, "is_hole": bool(is_hole)})

    def fit_clearance(self, process: str, fit_class: str) -> float:
        return float(self._call("dfm_fit_clearance",
                                {"process": process, "fit_class": fit_class}))


process_profiles = _ProcessProfilesBridge()


# Confidence tiers, ordered weakest -> strongest. ``required_tier`` and ``is_satisfied`` compare
# achieved-vs-required by index in this ladder, so the order here IS the rule.
TIER_ORDER: tuple[str, ...] = ("low", "medium", "high", "measured")

#: Per-tier uncertainty the scorer reports when it lands on a tier with no finer estimate (mm).
#: Coarser tiers carry larger ±; ``measured`` is the typed-caliper floor.
TIER_UNCERT_MM: dict[str, float] = {
    "measured": 0.05,
    "high": 0.30,
    "medium": 0.60,
    "low": 1.20,
}


def tier_index(tier: str) -> int:
    """Index of ``tier`` in :data:`TIER_ORDER`; unknown tiers sort as the weakest (-1)."""
    t = str(tier or "").strip().lower()
    return TIER_ORDER.index(t) if t in TIER_ORDER else -1


def worst_tier(tiers: list[str]) -> str:
    """Return the weakest confidence tier from a list (for aggregating per-view scores)."""
    if not tiers:
        return "low"
    return min(tiers, key=tier_index)


def _tier_index(tier: str) -> int:
    """Backward-compatible alias for :func:`tier_index`."""
    return tier_index(tier)


# ===========================================================================================
# 1. SCHEMAS — the ONE canonical §2 record set. Dataclasses, each with a new_* factory and
#    JSON-round-trippable to_dict / from_dict. These persist as opaque top-level doc keys
#    (cloned from the `studies` machinery), so they must stay plain-JSON shaped.
# ===========================================================================================


@dataclass
class MeasuredFeature:
    """One named mating feature on a real-world part (a thing that must *fit*).

    Mirrors an entry of ``measured_parts[].features[]`` in the plan's §2 schema. ``dims`` holds
    the archetype-specific measurements (``{"dia_mm":3.0}``, ``{"spacing_mm":18.0,"count":2}``,
    ``{"x_mm":24.1,"y_mm":31.0}`` …) so one record shape covers every archetype.
    """

    key: str
    archetype: str
    dims: dict[str, Any] = field(default_factory=dict)
    fit: str = "clearance"
    confidence: str = "low"
    uncert_mm: Optional[float] = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "archetype": self.archetype,
            "dims": copy.deepcopy(self.dims),
            "fit": self.fit,
            "confidence": self.confidence,
            "uncert_mm": self.uncert_mm,
            "provenance": copy.deepcopy(self.provenance),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MeasuredFeature":
        return cls(
            key=d["key"],
            archetype=d.get("archetype", "unknown"),
            dims=copy.deepcopy(d.get("dims") or {}),
            fit=d.get("fit", "clearance"),
            confidence=d.get("confidence", "low"),
            uncert_mm=d.get("uncert_mm"),
            provenance=copy.deepcopy(d.get("provenance") or {}),
        )


def new_measured_feature(key: str, archetype: str, *, dims: Optional[dict] = None,
                         fit: Optional[str] = None, confidence: str = "low",
                         uncert_mm: Optional[float] = None,
                         provenance: Optional[dict] = None) -> MeasuredFeature:
    """Build a :class:`MeasuredFeature`, defaulting ``fit`` from the archetype's taxonomy entry."""
    if fit is None:
        spec = ARCHETYPES.get(archetype)
        fit = spec.default_fit if spec else "clearance"
    return MeasuredFeature(
        key=key, archetype=archetype, dims=dict(dims or {}), fit=fit,
        confidence=confidence, uncert_mm=uncert_mm, provenance=dict(provenance or {}),
    )


@dataclass
class MeasuredPartProfile:
    """A real-world part we have no model for, reconstructed from photos / vendor / catalog / user.

    Mirrors ``measured_parts[]`` in §2. ``datum`` lets the modeler seat it without a measure
    round-trip (same shape ``make_standard_part`` returns).
    """

    id: str
    label: str
    source: str = "photo"          # photo|vendor|catalog|user
    images: list[dict[str, Any]] = field(default_factory=list)
    envelope: dict[str, Any] = field(default_factory=dict)
    features: list[MeasuredFeature] = field(default_factory=list)
    datum: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "images": copy.deepcopy(self.images),
            "envelope": copy.deepcopy(self.envelope),
            "features": [f.to_dict() for f in self.features],
            "datum": copy.deepcopy(self.datum),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MeasuredPartProfile":
        return cls(
            id=d["id"],
            label=d.get("label", ""),
            source=d.get("source", "photo"),
            images=copy.deepcopy(d.get("images") or []),
            envelope=copy.deepcopy(d.get("envelope") or {}),
            features=[MeasuredFeature.from_dict(f) for f in (d.get("features") or [])],
            datum=copy.deepcopy(d.get("datum") or {}),
        )

    def feature(self, key: str) -> Optional[MeasuredFeature]:
        """Return the feature with ``key``, or ``None``."""
        for f in self.features:
            if f.key == key:
                return f
        return None


def new_measured_part(part_id: str, label: str, *, source: str = "photo",
                      images: Optional[list] = None, envelope: Optional[dict] = None,
                      features: Optional[list[MeasuredFeature]] = None,
                      datum: Optional[dict] = None) -> MeasuredPartProfile:
    """Build a :class:`MeasuredPartProfile` (empty feature/image lists by default)."""
    return MeasuredPartProfile(
        id=part_id, label=label, source=source,
        images=list(images or []), envelope=dict(envelope or {}),
        features=list(features or []), datum=dict(datum or {}),
    )


@dataclass
class CriticalDimension:
    """One row of the critical-dimension ledger — a single dimension and whether it gates fit.

    Mirrors ``crit_dims[]`` in §2. ``is_critical`` is **deterministic** (it comes from the
    taxonomy, not an LLM vibe). ``status`` walks ``unknown -> given|measured|standard|assumed
    |stacked``; ``required_tier`` scales with fit + process (§5).
    """

    id: str
    key: str                       # e.g. "output_shaft.dia"
    feature: str
    archetype: str
    is_critical: bool = True
    required_tier: str = "high"
    status: str = "unknown"        # unknown|given|measured|standard|assumed|stacked
    resolved_by: Optional[str] = None   # user_confirmed|vision_high|reference|assumed
    nominal_mm: Optional[float] = None
    fit: str = "clearance"
    stackup: dict[str, Any] = field(default_factory=dict)
    blocks_features: list[str] = field(default_factory=list)
    why_it_matters: str = ""
    suggested: Optional[float] = None
    audit: dict[str, Any] = field(default_factory=dict)  # {who, ts, superseded_value, reason}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "feature": self.feature,
            "archetype": self.archetype,
            "is_critical": self.is_critical,
            "required_tier": self.required_tier,
            "status": self.status,
            "resolved_by": self.resolved_by,
            "nominal_mm": self.nominal_mm,
            "fit": self.fit,
            "stackup": copy.deepcopy(self.stackup),
            "blocks_features": list(self.blocks_features),
            "why_it_matters": self.why_it_matters,
            "suggested": self.suggested,
            "audit": copy.deepcopy(self.audit),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CriticalDimension":
        return cls(
            id=d.get("id", ""),
            key=d["key"],
            feature=d.get("feature", ""),
            archetype=d.get("archetype", "unknown"),
            is_critical=bool(d.get("is_critical", True)),
            required_tier=d.get("required_tier", "high"),
            status=d.get("status", "unknown"),
            resolved_by=d.get("resolved_by"),
            nominal_mm=d.get("nominal_mm"),
            fit=d.get("fit", "clearance"),
            stackup=copy.deepcopy(d.get("stackup") or {}),
            blocks_features=list(d.get("blocks_features") or []),
            why_it_matters=d.get("why_it_matters", ""),
            suggested=d.get("suggested"),
            audit=copy.deepcopy(d.get("audit") or {}),
        )


def new_critical_dimension(cd_id: str, key: str, feature: str, archetype: str, *,
                           required_tier: str = "high", fit: str = "clearance",
                           nominal_mm: Optional[float] = None,
                           blocks_features: Optional[list[str]] = None,
                           why_it_matters: str = "",
                           suggested: Optional[float] = None) -> CriticalDimension:
    """Build a fresh, unresolved :class:`CriticalDimension` (status ``unknown``)."""
    return CriticalDimension(
        id=cd_id, key=key, feature=feature, archetype=archetype,
        is_critical=True, required_tier=required_tier, status="unknown",
        resolved_by=None, nominal_mm=nominal_mm, fit=fit, stackup={},
        blocks_features=list(blocks_features or []),
        why_it_matters=why_it_matters, suggested=suggested,
    )


@dataclass
class CriticalDimensionLedger:
    """The gate's source of truth: a list of :class:`CriticalDimension` + computed views.

    The §2 "computed views (not stored)" — :func:`unresolved_critical` and :func:`assumptions` —
    are exposed as free functions (and convenience methods) so they are never persisted stale.
    The ledger carries the bound ``process`` id so demand/fit math can reach the right profile.
    """

    rows: list[CriticalDimension] = field(default_factory=list)
    process: str = process_profiles.DEFAULT_PROCESS

    def to_dict(self) -> dict[str, Any]:
        return {"process": self.process, "rows": [r.to_dict() for r in self.rows]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CriticalDimensionLedger":
        return cls(
            rows=[CriticalDimension.from_dict(r) for r in (d.get("rows") or [])],
            process=d.get("process", process_profiles.DEFAULT_PROCESS),
        )

    def row(self, key: str) -> Optional[CriticalDimension]:
        for r in self.rows:
            if r.key == key:
                return r
        return None

    # convenience pass-throughs to the module-level computed views
    def unresolved_critical(self) -> list[CriticalDimension]:
        return unresolved_critical(self)

    def gate_ok(self) -> bool:
        return gate_ok(self)

    def assumptions(self) -> list[str]:
        return assumptions(self)


@dataclass
class FitReport:
    """The review artifact: every mating pair's modeled-vs-source dim, provenance, and verdict.

    Mirrors ``fit_report`` in §2. ``overall`` is ``pass|at_risk``; each check verdict is one of
    ``ok|tight|risky|unmeasured``.
    """

    checks: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    process: str = process_profiles.DEFAULT_PROCESS
    overall: str = "pass"          # pass|at_risk
    coupon: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": copy.deepcopy(self.checks),
            "assumptions": list(self.assumptions),
            "process": self.process,
            "overall": self.overall,
            "coupon": copy.deepcopy(self.coupon),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FitReport":
        return cls(
            checks=copy.deepcopy(d.get("checks") or []),
            assumptions=list(d.get("assumptions") or []),
            process=d.get("process", process_profiles.DEFAULT_PROCESS),
            overall=d.get("overall", "pass"),
            coupon=copy.deepcopy(d.get("coupon") or {}),
        )


def new_fit_report(process: str = process_profiles.DEFAULT_PROCESS) -> FitReport:
    """Build an empty, passing :class:`FitReport` bound to ``process``."""
    return FitReport(checks=[], assumptions=[], process=process, overall="pass", coupon={})


@dataclass
class IntakeState:
    """The resume token persisted in the doc so suspend/resume is stateless across restarts.

    Mirrors ``intake`` in §2. ``status`` is ``open|awaiting_user|resolved``; each ``needs`` entry
    is ``{kind, key, ask, unit, suggested}`` where ``kind`` is ``photo|dimension_confirm
    |process_clarify``.
    """

    status: str = "open"           # open|awaiting_user|resolved
    needs: list[dict[str, Any]] = field(default_factory=list)
    pending_request_id: Optional[str] = None
    doc_id: Optional[str] = None
    revision: int = 0
    # Doc-scoped interaction Q/A records (contracts/interaction.md §5):
    # {"questions": [<question dict>...], "answers": {qid: <answer dict>}}.
    # Same records as the run-scoped FileLedger; different store — persisted
    # with this state into the .step+ so a confirmed answer survives reopen.
    interaction: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "needs": copy.deepcopy(self.needs),
            "pending_request_id": self.pending_request_id,
            "doc_id": self.doc_id,
            "revision": self.revision,
            "interaction": copy.deepcopy(self.interaction),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IntakeState":
        return cls(
            status=d.get("status", "open"),
            needs=copy.deepcopy(d.get("needs") or []),
            pending_request_id=d.get("pending_request_id"),
            doc_id=d.get("doc_id"),
            revision=int(d.get("revision", 0)),
            interaction=copy.deepcopy(d.get("interaction") or {}),
        )


def new_intake_state(*, doc_id: Optional[str] = None, revision: int = 0) -> IntakeState:
    """Build a fresh, ``open`` :class:`IntakeState`."""
    return IntakeState(status="open", needs=[], pending_request_id=None,
                       doc_id=doc_id, revision=revision)


# ===========================================================================================
# 2. MATING-FEATURE TAXONOMY — the deterministic critical/cosmetic brain. Every archetype is a
#    *mate*, so every archetype is is_critical=True; criticality is taxonomy-driven, never an
#    LLM guess (plan §3, §4). classify() maps plain words -> an archetype.
# ===========================================================================================


@dataclass(frozen=True)
class Archetype:
    """One mating-feature archetype: the dims that gate its fit + its default fit class.

    * ``critical_keys`` — the dimension keys that decide whether the mate works (seeded
      ``unknown`` in the ledger; these are what we demand or measure).
    * ``default_fit``   — the fit class (slip/location/press/clearance/free) the modeler applies
      to size the receptacle (feeds ``process_profiles.modeled_dim``).
    * ``is_critical``   — always True: an archetype *is* a mate.
    * ``label``         — human phrase for asks / reports.
    """

    name: str
    critical_keys: tuple[str, ...]
    default_fit: str
    label: str
    is_critical: bool = True


#: The mating-feature taxonomy (plan §1 step 4 + the spec's eight archetypes). Each key gating a
#: fit is named ``<feature>.<dim>``-style at ledger-build time; here we list the bare dim names.
ARCHETYPES: dict[str, Archetype] = {
    # a shaft passing through / seating in a bore -> slip fit on the bore diameter
    "shaft_in_bore": Archetype(
        "shaft_in_bore", ("dia",), "slip", "shaft-in-bore"),
    # a pattern of mounting screws -> the spacing AND hole diameter must locate (positional)
    "bolt_pattern": Archetype(
        "bolt_pattern", ("spacing", "dia"), "location", "bolt pattern"),
    # a rectangular body dropping into a pocket -> overall x/y with a clearance fit
    "prism_pocket": Archetype(
        "prism_pocket", ("x", "y"), "clearance", "prism pocket"),
    # a connector cutout (USB/HDMI/DC jack) -> the opening width/height must clear the housing
    "connector_cutout": Archetype(
        "connector_cutout", ("width", "height"), "clearance", "connector cutout"),
    # a PCB outline + mounting holes -> the board outline x/y must seat in its bay
    "board_outline": Archetype(
        "board_outline", ("x", "y"), "clearance", "board outline"),
    # a snap-fit (cantilever/annular) -> the engaging dimension gates retention (location)
    "snap_fit": Archetype(
        "snap_fit", ("dia",), "location", "snap fit"),
    # a lid mating to a base -> the mating lip/diameter must register (location)
    "lid_mate": Archetype(
        "lid_mate", ("dia",), "location", "lid mate"),
    # a boss pressed onto a post / into a hole -> press class on the engaging diameter
    "press_boss": Archetype(
        "press_boss", ("dia",), "press", "press boss"),
}

# Plain-word -> archetype keyword table for classify(). Ordered most-specific first so a
# multi-word phrase ("usb port") wins before a generic token. Each tuple is (substring, archetype).
_CLASSIFY_KEYWORDS: tuple[tuple[str, str], ...] = (
    # connectors / ports
    ("usb-c", "connector_cutout"), ("usb c", "connector_cutout"), ("usb", "connector_cutout"),
    ("hdmi", "connector_cutout"), ("micro usb", "connector_cutout"),
    ("dc jack", "connector_cutout"), ("barrel jack", "connector_cutout"),
    ("connector", "connector_cutout"), ("port", "connector_cutout"),
    ("cutout", "connector_cutout"), ("opening", "connector_cutout"),
    # board / PCB
    ("pcb", "board_outline"), ("circuit board", "board_outline"),
    ("board outline", "board_outline"), ("board", "board_outline"),
    # bolt / screw patterns
    ("bolts on", "bolt_pattern"), ("bolt pattern", "bolt_pattern"),
    ("bolt circle", "bolt_pattern"), ("mounting holes", "bolt_pattern"),
    ("mount holes", "bolt_pattern"), ("screw holes", "bolt_pattern"),
    ("screws", "bolt_pattern"), ("bolts", "bolt_pattern"), ("bolt", "bolt_pattern"),
    ("hole pattern", "bolt_pattern"), ("hole spacing", "bolt_pattern"),
    # shaft / bore
    ("output shaft", "shaft_in_bore"), ("shaft", "shaft_in_bore"),
    ("axle", "shaft_in_bore"), ("pin passthrough", "shaft_in_bore"),
    ("passthrough", "shaft_in_bore"), ("through hole", "shaft_in_bore"),
    ("bore", "shaft_in_bore"), ("bearing", "shaft_in_bore"),
    # press boss / post
    ("press fit", "press_boss"), ("press-fit", "press_boss"),
    ("press boss", "press_boss"), ("boss", "press_boss"), ("standoff", "press_boss"),
    # snap fit
    ("snap fit", "snap_fit"), ("snap-fit", "snap_fit"), ("snap", "snap_fit"),
    ("clip", "snap_fit"), ("latch", "snap_fit"),
    # lid / cover mate
    ("lid", "lid_mate"), ("cover mate", "lid_mate"), ("cap", "lid_mate"),
    ("twist mate", "lid_mate"),
    # pocket / slot / recess (generic prism receptacle) — last, most generic
    ("pocket", "prism_pocket"), ("slot", "prism_pocket"), ("recess", "prism_pocket"),
    ("cavity", "prism_pocket"), ("body pocket", "prism_pocket"),
    ("seat", "prism_pocket"), ("nest", "prism_pocket"),
)


def classify(feature_request_text: str) -> Optional[str]:
    """Map a plain-English feature description to a taxonomy archetype, or ``None`` if unrecognized.

    Heuristic, deterministic, explainable — substring keywords ordered most-specific-first
    (``"output shaft" -> shaft_in_bore``, ``"bolts on with two screws" -> bolt_pattern``,
    ``"USB port" -> connector_cutout``, ``"holds the board" -> board_outline``). Returns the
    archetype *name* (a key of :data:`ARCHETYPES`) so the caller can look up its fit + critical_keys.
    """
    t = str(feature_request_text or "").strip().lower()
    if not t:
        return None
    for kw, arch in _CLASSIFY_KEYWORDS:
        if kw in t:
            return arch
    return None


# ===========================================================================================
# 3. CRITICAL-DIM LEDGER — seed each archetype's critical_keys as unknown; the §3 gate views.
# ===========================================================================================


def _dim_value(feature_dims: dict[str, Any], dim: str) -> Optional[float]:
    """Pull the measured value for a bare dim name from a feature's ``dims`` dict.

    Accepts both bare (``"dia"``) and suffixed (``"dia_mm"``) keys so it works against either the
    taxonomy's critical_keys or the §2 ``dims`` shape (which stores ``dia_mm``/``spacing_mm`` …).
    """
    if dim in feature_dims:
        return _as_float(feature_dims[dim])
    suffixed = f"{dim}_mm"
    if suffixed in feature_dims:
        return _as_float(feature_dims[suffixed])
    return None


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_ledger(measured: Any, process: str = process_profiles.DEFAULT_PROCESS,
                 ) -> CriticalDimensionLedger:
    """Seed a :class:`CriticalDimensionLedger` from a measured part *or* a bare feature list.

    For every feature, look up its archetype in :data:`ARCHETYPES` and seed **one ledger row per
    ``critical_key``**, ``status="unknown"`` (the §1-step-4 "each archetype's critical_keys are
    seeded unknown"). Criticality is taxonomy-deterministic. ``required_tier`` is computed from the
    feature's fit class and the bound process (§5 scaling). If a feature already carries a measured
    value for a key, the row is seeded ``unknown`` regardless — :func:`apply_confirmation` /
    :func:`compute_demands` decide resolution, keeping seeding side-effect-free.

    ``measured`` may be a :class:`MeasuredPartProfile`, its ``to_dict``, or a plain list of
    features (dicts or :class:`MeasuredFeature`). Unknown archetypes are skipped (nothing to gate).
    """
    features = _coerce_features(measured)
    ledger = CriticalDimensionLedger(rows=[], process=_norm_process(process))
    n = 0
    for feat in features:
        spec = ARCHETYPES.get(feat.archetype)
        if spec is None:
            continue  # not a known mate -> nothing deterministic to gate
        for dim in spec.critical_keys:
            n += 1
            key = f"{feat.key}.{dim}"
            nominal = _dim_value(feat.dims, dim)
            req = required_tier(feat.fit, ledger.process)
            ledger.rows.append(new_critical_dimension(
                cd_id=f"cd_{n}",
                key=key,
                feature=feat.key,
                archetype=feat.archetype,
                required_tier=req,
                fit=feat.fit,
                nominal_mm=nominal,
                blocks_features=[feat.key],
                why_it_matters=_why_it_matters(feat.archetype, dim, spec),
                suggested=nominal,
            ))
    return ledger


def _why_it_matters(archetype: str, dim: str, spec: Archetype) -> str:
    """Plain-English reason a given critical dim gates fit (for asks + the FitReport)."""
    phrases = {
        "shaft_in_bore": "the shaft won't pass if the bore is wrong",
        "bolt_pattern": ("it won't bolt on if the hole spacing or size is off"
                         if dim == "spacing" else "the screws won't pass if the holes are wrong"),
        "prism_pocket": "the body won't drop into the pocket if this is undersized",
        "connector_cutout": "the connector won't clear the opening if this is wrong",
        "board_outline": "the board won't seat in its bay if this is wrong",
        "snap_fit": "the snap won't engage/retain if this is wrong",
        "lid_mate": "the lid won't register/seal if this is wrong",
        "press_boss": "the press fit will be loose or split if this is wrong",
    }
    return phrases.get(archetype, f"{spec.label} {dim} gates the mate")


def unresolved_critical(ledger: CriticalDimensionLedger) -> list[CriticalDimension]:
    """The §2 computed view: rows where ``is_critical and status == 'unknown'``.

    These are the dims that *block* fit-critical geometry until resolved (the §3 critical-dim gate).
    """
    return [r for r in ledger.rows if r.is_critical and r.status == "unknown"]


def apply_confirmation(ledger: CriticalDimensionLedger, key: str, value: Any, *,
                       resolved_by: Optional[str] = None,
                       status: Optional[str] = None,
                       who: Optional[str] = None,
                       ts: Optional[str] = None,
                       superseded_value: Optional[Any] = None,
                       reason: Optional[str] = None) -> CriticalDimension:
    """Resolve a ledger row from a user-supplied value (the demand-closing path).

    Sets ``nominal_mm = value`` and marks it resolved. ``status`` defaults to ``"given"`` (a typed
    user value); pass ``"measured"`` for a typed caliper number that closes a ``measured``-tier
    demand. ``resolved_by`` defaults to ``"user_confirmed"`` — the plan's provenance for a typed
    value. Raises ``KeyError`` if no row matches ``key``.
    """
    row = ledger.row(key)
    if row is None:
        raise KeyError(f"no critical-dim row for key {key!r}")
    row.nominal_mm = _as_float(value)
    row.suggested = row.nominal_mm if row.suggested is None else row.suggested
    row.status = status or "given"
    row.resolved_by = resolved_by or "user_confirmed"
    # Audit trail — stamp who/when/why changed a value. Absent fields default on old docs.
    audit: dict[str, Any] = {}
    if who is not None:
        audit["who"] = who
    if ts is not None:
        audit["ts"] = ts
    if superseded_value is not None:
        audit["superseded_value"] = superseded_value
    if reason is not None:
        audit["reason"] = reason
    row.audit = audit
    return row


def gate_ok(ledger: CriticalDimensionLedger) -> bool:
    """True when no critical dim is still ``unknown`` — the §3 gate clears (fit geometry may commit)."""
    return not unresolved_critical(ledger)


def assumptions(ledger: CriticalDimensionLedger) -> list[str]:
    """The §2 ``assumed_flags()`` computed view -> FitReport assumption lines.

    One line per row resolved by assumption/default (``status in {assumed}`` or
    ``resolved_by == 'assumed'``), so the FitReport can lead with every soft guess (§5 calibration
    note: "lead the FitReport with the assumed band").
    """
    out: list[str] = []
    for r in ledger.rows:
        if r.status == "assumed" or r.resolved_by == "assumed":
            val = f" (assumed {r.nominal_mm:g} mm)" if r.nominal_mm is not None else ""
            out.append(f"{r.key}{val} — {r.why_it_matters}")
    return out


# ===========================================================================================
# 4. CONFIDENCE SCORER + DEMAND RULE — the plan's heart (§5). Deterministic tier from concrete
#    signals; required_tier scales with fit + process; compute_demands flags critical dims whose
#    achieved confidence is below what the fit demands.
# ===========================================================================================

#: Scale-reference quality, best -> worst. A printed/ArUco card homography beats a typed caliper
#: reading off a feature, which beats a ruler in frame, which beats a coin, which beats nothing.
_SCALE_RANK: dict[str, int] = {
    "card": 4, "card_homography": 4,
    "caliper_typed": 3, "caliper": 3,
    "ruler": 2, "coin": 1,
    "none": 0, "": 0,
}


def _scale_rank(src: Any) -> int:
    return _SCALE_RANK.get(str(src or "").strip().lower(), 0)


def score_claim(claim: dict[str, Any]) -> dict[str, Any]:
    """Deterministically score one measurement claim -> ``{tier, uncert_mm}``.

    Concrete signals (the plan's §1-step-7 / §5 rubric — NO LLM vibe):

    * ``scale_source``        — ``card`` > ``caliper_typed`` > ``ruler`` > ``coin`` > ``none``.
    * ``in_plane``            — the scale ref sat in the measured feature's plane (no foreshortening).
    * ``agrees_across_views`` — the dim read the same in two ortho views.
    * ``edge_quality``        — ``sharp`` edges read true; ``soft`` ones lose a tier of trust.
    * ``caliper_readout``     — a typed caliper number present -> instantly ``measured`` tier.

    A typed caliper number is the gold standard and short-circuits to ``measured`` (the plan never
    trusts OCR on the one dim that decides fit; a typed value *is* that dim). Otherwise we start
    from the scale-source rank and adjust for plane / cross-view / edge crispness, then clamp into
    :data:`TIER_ORDER`. ``uncert_mm`` is the explicit ``uncert_mm`` on the claim if given, else the
    per-tier default from :data:`TIER_UNCERT_MM`.
    """
    # A typed caliper number is provider-independent ground truth -> measured, full stop. Note we
    # require the actual readout, not merely a "caliper_typed" scale source: the plan never grants
    # measured tier to a claim that lacks the one typed value that makes it trustworthy.
    if claim.get("caliper_readout") is not None:
        tier = "measured"
        uncert = _as_float(claim.get("uncert_mm"))
        return {"tier": tier, "uncert_mm": uncert if uncert is not None else TIER_UNCERT_MM[tier]}

    rank = _scale_rank(claim.get("scale_source"))
    # base tier from scale source: card/caliper-ref -> high, ruler -> medium, coin -> low, none ->
    # below-low. (A "caliper_typed" SCALE SOURCE without an actual readout — rank 3 — is treated as
    # a strong card-class reference, not auto-promoted to measured; only a typed value does that.)
    if rank >= 3:
        score = 2          # high (printed card homography / caliper-grade reference in frame)
    elif rank == 2:
        score = 1          # medium (ruler in frame)
    elif rank == 1:
        score = 0          # low (coin)
    else:
        score = -1         # none -> below low; clamps up to low

    # corroborating signals push the tier up; weak ones pull it down
    if claim.get("in_plane"):
        score += 0
    else:
        score -= 1         # out-of-plane foreshortens -> one tier of trust lost
    if claim.get("agrees_across_views"):
        score += 1         # two ortho views agree -> a tier of corroboration
    eq = str(claim.get("edge_quality", "")).strip().lower()
    if eq == "soft":
        score -= 1
    elif eq == "sharp":
        score += 0

    # map the integer score onto low/medium/high (measured only via typed caliper above)
    if score >= 2:
        tier = "high"
    elif score == 1:
        tier = "medium"
    else:
        tier = "low"

    uncert = _as_float(claim.get("uncert_mm"))
    return {"tier": tier, "uncert_mm": uncert if uncert is not None else TIER_UNCERT_MM[tier]}


# Which confidence tier each fit class demands (plan §5 "required-tier scaling"). press/bearing/
# threaded -> a typed caliper number (measured); slip -> high; cosmetic/bulk -> medium.
_FIT_REQUIRED_TIER: dict[str, str] = {
    "press": "measured",
    "interference": "measured",
    "bearing": "measured",
    "threaded": "measured",
    "thread": "measured",
    "slip": "high",
    "location": "high",
    "clearance": "medium",
    "free": "medium",
    "cosmetic": "medium",
}

# Fit classes that are "tolerance-sensitive" for the §5 photo-downgrade rule. Per the plan's
# literal example ("press -> location") the downgrade targets the genuinely *tight* interference-
# class fits — the ones a photo can never honestly satisfy. We deliberately do NOT downgrade slip
# or location: the §1 worked example keeps the shaft (slip, required `high`) and the hole spacing
# (location, required `high`) DEMANDING a typed value when vision is only low/medium. Softening
# those would silently skip the two demands the journey is built around.
_TOLERANCE_SENSITIVE: frozenset[str] = frozenset(
    {"press", "interference", "bearing", "threaded", "thread"})

# One fit step down, for the photo-downgrade rule (press -> location, per §5's literal example).
_FIT_DOWNGRADE: dict[str, str] = {
    "press": "location", "interference": "location", "bearing": "location",
    "threaded": "location", "thread": "location",
}


def required_tier(fit_class: str, process: str = process_profiles.DEFAULT_PROCESS,
                  *, from_photo: bool = False) -> str:
    """The confidence tier a fit class demands on a process (§5 required-tier scaling).

    press/bearing/threaded -> ``measured`` (typed caliper); slip/location -> ``high``;
    clearance/cosmetic/bulk -> ``medium``. When ``from_photo`` is True and the fit is
    tolerance-sensitive, the fit class is **downgraded one step** before mapping (the plan's
    "a photo dim feeding a tolerance-sensitive fit gets its fit class downgraded one step and
    flagged"), softening the demand a photo can't honestly meet — unless a typed value closes it.

    ``process`` participates because a process with no diametral fit class (sheet/laser) can only
    ever demand ``medium`` for what would otherwise be a tight fit — there is no tight fit to gate.
    """
    fc = str(fit_class or "").strip().lower()
    if from_photo and fc in _TOLERANCE_SENSITIVE:
        fc = _FIT_DOWNGRADE.get(fc, fc)
    base = _FIT_REQUIRED_TIER.get(fc, "medium")

    # a flat/sheet process has no diametral tight fit -> nothing tighter than medium is meaningful
    proc = _norm_process(process)
    prof = process_profiles.PROFILES.get(proc)
    if prof is not None and base in ("measured", "high"):
        fits = prof.fit_classes_mm
        if all(v is None for v in fits.values()):
            return "medium"
    return base


def is_satisfied(achieved_tier: str, required: str) -> bool:
    """True when an achieved confidence tier meets or exceeds the required tier (ladder compare)."""
    return _tier_index(achieved_tier) >= _tier_index(required)


def compute_demands(measured: Any, ledger: CriticalDimensionLedger,
                    process: Optional[str] = None) -> list[dict[str, Any]]:
    """List the critical dims whose achieved confidence is below what their fit demands.

    For every still-unresolved critical row, find the measured feature's claim/confidence for that
    dim, compare achieved-vs-required tier, and — if it falls short — emit a **demand** for a typed
    caliper value (the plan's §1-step-8 rule: "we never trust OCR on the one dim that decides fit").
    A row already resolved by a typed value (``status in {given,measured}``) raises no demand.

    Each demand is ``{key, feature, archetype, fit, required_tier, achieved_tier, nominal_mm,
    uncert_mm, kind:"dimension_confirm", ask, suggested, why_it_matters}`` — ready to drop straight
    into :class:`IntakeState`.``needs``. Cosmetic / non-critical dims never produce a demand.
    """
    part = _coerce_part(measured)
    proc = _norm_process(process or ledger.process)
    demands: list[dict[str, Any]] = []
    for row in ledger.rows:
        if not row.is_critical:
            continue
        if row.status in ("given", "measured", "standard"):
            continue  # already closed by a typed/reference value
        feat = part.feature(row.feature) if part else None
        dim = row.key.split(".", 1)[1] if "." in row.key else row.key

        achieved = "low"
        uncert = None
        from_photo = False
        if feat is not None:
            achieved = feat.confidence or "low"
            uncert = feat.uncert_mm
            method = str((feat.provenance or {}).get("method", "")).lower()
            from_photo = method not in ("caliper_typed", "user_confirmed", "reference", "vendor",
                                        "catalog")

        req = required_tier(row.fit, proc, from_photo=from_photo)
        # keep the ledger's recorded required_tier in sync with the photo-aware computation
        row.required_tier = req
        if is_satisfied(achieved, req):
            continue

        demands.append({
            "key": row.key,
            "feature": row.feature,
            "archetype": row.archetype,
            "fit": row.fit,
            "required_tier": req,
            "achieved_tier": achieved,
            "nominal_mm": row.nominal_mm,
            "uncert_mm": uncert,
            "kind": "dimension_confirm",
            "unit": "mm",
            "ask": _demand_ask(row, dim, req),
            "suggested": row.suggested if row.suggested is not None else row.nominal_mm,
            "why_it_matters": row.why_it_matters,
        })
    return demands


def _demand_ask(row: CriticalDimension, dim: str, req: str) -> str:
    """The typed-value ask for one shortfall demand (calipers if measured-tier)."""
    pretty = ARCHETYPES.get(row.archetype)
    label = pretty.label if pretty else row.archetype
    if req == "measured":
        return (f"Put calipers on the {label} {dim} and type the number — "
                f"{row.why_it_matters}; I won't guess the dim that decides fit.")
    return (f"Confirm the {label} {dim} (type a value) — {row.why_it_matters}.")


# ===========================================================================================
# 5. PHOTO-REQUEST BUILDER — the templated, prioritized §1-step-5 ask. Pure text generation.
# ===========================================================================================

#: The fixed scale-reference options offered with every photo request (§4 / "what NOT to build":
#: static printable card + ruler/coin/typed-caliper — no homography dependency).
_SCALE_OPTIONS: tuple[str, ...] = ("card", "caliper", "ruler", "coin")


def build_photo_request(label: str, critical_features: list[Any],
                        process: str = process_profiles.DEFAULT_PROCESS) -> dict[str, Any]:
    """Build the prioritized, templated photo-request ask (plan §1 step 5). Pure — no I/O.

    ``critical_features`` is a list of the mating features that must fit — each a
    :class:`MeasuredFeature`, its dict, or ``{"key","archetype","fit"}``. The ask:

    * names *only* the things that actually have to fit (criticality is taxonomy-deterministic),
    * asks for the scale reference **in-plane** with the measured feature (no foreshortening),
    * asks for ortho views (top for footprint, front+side for height),
    * asks for a **typed caliper** number on any feature whose fit demands ``measured`` tier — the
      plan's "have calipers ready" for the dims that decide fit.

    Returns ``{ask_text, views, scale_options, request_id_seed, criticals, process}`` where
    ``request_id_seed`` is a deterministic, content-derived seed (the caller mints the durable
    ``pending_request_id`` from it; we do no I/O / randomness).
    """
    proc = _norm_process(process)
    feats = [_coerce_feature(f) for f in (critical_features or [])]
    feats = [f for f in feats if f is not None]

    names = [_feature_phrase(f) for f in feats]
    caliper_feats = [
        _feature_phrase(f) for f in feats
        if required_tier(f.fit, proc) == "measured"
        or f.archetype in ("shaft_in_bore", "bolt_pattern", "press_boss")
    ]

    views = ["top", "front", "side"]
    # close-up views for each critical feature so soft edges get a dedicated shot
    closeups = [f"closeup:{f.key}" for f in feats]
    views = views + closeups

    # ---- assemble the human ask ----------------------------------------------------
    lead = (f"I couldn't find a standard model for the {label}, so I'll measure it from photos. ")
    if names:
        if len(names) == 1:
            things = names[0]
        elif len(names) == 2:
            things = f"{names[0]} and {names[1]}"
        else:
            things = ", ".join(names[:-1]) + f", and {names[-1]}"
        lead += f"The things that actually have to fit: {things}. "
    parts = [lead]
    parts.append(
        "Send: (1) a TOP photo of it lying flat with a scale reference (printed card, ruler, or a "
        "coin) IN THE SAME PLANE as the part, (2) FRONT and SIDE photos for height, "
        "(3) a close-up of each feature above with the scale reference in that feature's plane.")
    parts.append(
        "Keep the camera straight-on (no angle) so nothing foreshortens, and get the scale "
        "reference flat in the same plane as what you're measuring.")
    if caliper_feats:
        if len(caliper_feats) == 1:
            cl = caliper_feats[0]
        else:
            cl = ", ".join(caliper_feats[:-1]) + f", and {caliper_feats[-1]}"
        parts.append(
            f"For {cl} I'll need actual caliper numbers — the photo is only corroboration on the "
            f"dims that decide fit. Have calipers ready and type those values.")

    ask_text = "\n".join(parts)
    seed_basis = "|".join([_norm(label), proc] + sorted(f.key for f in feats))
    request_id_seed = f"req_{abs(hash(seed_basis)) % 10_000_000:07d}"

    return {
        "ask_text": ask_text,
        "views": views,
        "scale_options": list(_SCALE_OPTIONS),
        "request_id_seed": request_id_seed,
        "criticals": [f.key for f in feats],
        "process": proc,
    }


def _feature_phrase(feat: MeasuredFeature) -> str:
    """Human phrase for a feature in an ask (archetype label + a readable key)."""
    spec = ARCHETYPES.get(feat.archetype)
    label = spec.label if spec else feat.archetype
    nice = feat.key.replace("_", " ")
    if nice and nice.lower() not in label.lower():
        return f"the {nice} ({label})"
    return f"the {label}"


# ===========================================================================================
# 6. FIT REPORT + COUPON — the review artifact (§1 step 10) + the fit-test coupon (§6 gate 3).
# ===========================================================================================

# verdict ladder for a single fit check; worse verdicts dominate the overall roll-up
_VERDICT_RANK: dict[str, int] = {"ok": 0, "tight": 1, "risky": 2, "unmeasured": 3}


def build_fit_report(measured: Any, ledger: CriticalDimensionLedger,
                     process: Optional[str] = None) -> FitReport:
    """Assemble the :class:`FitReport` — one check per mating pair, modeled-vs-source + verdict.

    For each critical-dim row that carries a nominal, compute the **modeled** receptacle dimension
    via :func:`process_profiles.modeled_dim` (so the clearance is exactly what the planner commits),
    record the source value + provenance/confidence, and assign a verdict:

    * ``ok``         — resolved (typed/measured/reference) and the clearance is applied.
    * ``tight``      — resolved but the fit class is tight (press/location) on an FDM process where
                       interference is unreliable — flagged so the user eyes it.
    * ``risky``      — committed on a low/medium photo confidence below what the fit demands.
    * ``unmeasured`` — still ``unknown``; committed on a default (the never-block path).

    ``overall`` is ``at_risk`` if any check is worse than ``ok``, else ``pass``. ``assumptions``
    folds in the ledger's assumed-flags plus the process assumption. ``measured`` may be a part or
    its dict; ``process`` defaults to the ledger's bound process.
    """
    part = _coerce_part(measured)
    proc = _norm_process(process or ledger.process)
    report = new_fit_report(proc)

    worst = 0
    for row in ledger.rows:
        if not row.is_critical:
            continue
        feat = part.feature(row.feature) if part else None
        confidence = (feat.confidence if feat else None) or _confidence_from_status(row.status)
        source = _provenance_label(row, feat)

        if row.nominal_mm is None:
            check = {
                "feature": row.feature,
                "key": row.key,
                "modeled_mm": None,
                "source_mm": None,
                "fit": row.fit,
                "clearance_mm": None,
                "source": source,
                "confidence": confidence,
                "verdict": "unmeasured",
            }
        else:
            stack = process_profiles.modeled_dim(
                row.nominal_mm, _fit_for_math(row.fit, proc), proc,
                is_hole=row.archetype in ("shaft_in_bore", "bolt_pattern"))
            verdict = _verdict_for(row, confidence, proc)
            # cache the stackup back onto the ledger row (status -> stacked) for persistence/§2
            row.stackup = {
                "clearance_mm": stack["clearance_mm"],
                "modeled_mm": stack["modeled_mm"],
                "rationale": stack["rationale"],
            }
            if row.status in ("given", "measured", "standard", "assumed"):
                row.status = "stacked"
            check = {
                "feature": row.feature,
                "key": row.key,
                "modeled_mm": stack["modeled_mm"],
                "source_mm": row.nominal_mm,
                "fit": row.fit,
                "clearance_mm": stack["clearance_mm"],
                "source": source,
                "confidence": confidence,
                "verdict": verdict,
            }
        worst = max(worst, _VERDICT_RANK.get(check["verdict"], 0))
        report.checks.append(check)

    report.assumptions = list(assumptions(ledger))
    report.assumptions.append(f"process {proc.upper()} assumed")
    report.overall = "pass" if worst == 0 else "at_risk"
    return report


def _fit_for_math(fit: str, process: str) -> str:
    """Coerce a fit class to one the process's clearance table actually defines (fallback clearance)."""
    proc = _norm_process(process)
    prof = process_profiles.PROFILES.get(proc)
    fc = str(fit or "clearance").strip().lower()
    if prof is None:
        return "clearance" if fc not in ("slip", "location", "press", "free") else fc
    fits = prof.fit_classes_mm
    if fits.get(fc) is not None:
        return fc
    # pick the first defined class as a safe fallback (e.g. cosmetic -> free); else clearance-ish
    for cand in ("free", "slip", "location", "press"):
        if fits.get(cand) is not None:
            return cand
    return "free"


def _verdict_for(row: CriticalDimension, confidence: str, process: str) -> str:
    """Per-check verdict from resolution status + confidence + fit tightness."""
    req = required_tier(row.fit, process)
    if row.status == "unknown" and row.resolved_by != "assumed":
        return "unmeasured"
    # resolved by a typed/reference value -> trust it; flag only genuinely tight FDM fits
    if row.status in ("given", "measured", "standard", "stacked") or row.resolved_by in (
            "user_confirmed", "reference", "vision_high"):
        if process == "fdm" and row.fit in ("press", "interference"):
            return "tight"  # FDM interference is unreliable -> eye it (plan §5)
        return "ok"
    # committed on a soft/assumed value: risky if below what the fit demands
    if not is_satisfied(confidence, req):
        return "risky"
    return "ok"


def _confidence_from_status(status: str) -> str:
    return {"given": "measured", "measured": "measured", "standard": "high",
            "assumed": "low", "stacked": "measured"}.get(status, "low")


def _provenance_label(row: CriticalDimension, feat: Optional[MeasuredFeature]) -> str:
    """A short source label for a check (``measured`` / ``photo-high`` / ``assumed`` …)."""
    if row.resolved_by == "user_confirmed" or row.status in ("given", "measured"):
        return "measured"
    if row.resolved_by == "reference" or row.status == "standard":
        return "reference"
    if row.status == "assumed" or row.resolved_by == "assumed":
        return "assumed"
    if feat is not None:
        return f"photo-{feat.confidence}" if feat.confidence else "photo"
    return "unmeasured"


def coupon_spec(interface_feature: Any, process: str = process_profiles.DEFAULT_PROCESS,
                ) -> dict[str, Any]:
    """A printable fit-test coupon spec for one interface — an Chudware feature **op-list** (no geometry).

    Scoped to the plan's three easy archetypes (§6 gate 3): ``shaft_in_bore`` (bore),
    ``bolt_pattern`` (hole pattern), and ``connector_cutout``. Returns a small box plus the
    interface, expressed as the same op dicts a caller would feed to ``add_features`` — so the
    caller builds the ~20-min / ~5 g coupon and the user verifies real fit before the full part.

    The interface dimensions are **stacked** through :func:`process_profiles.modeled_dim` so the
    coupon's bore/holes carry the exact clearance the real part will. Returns
    ``{ok, archetype, est_print_min, est_mass_g, interface, ops, notes}``; ``ok=False`` with a
    reason for an archetype outside the supported three.
    """
    feat = _coerce_feature(interface_feature)
    if feat is None:
        return {"ok": False, "reason": "no interface feature supplied", "ops": []}
    proc = _norm_process(process)
    arch = feat.archetype
    supported = ("shaft_in_bore", "bolt_pattern", "connector_cutout")
    if arch not in supported:
        return {"ok": False, "archetype": arch, "ops": [],
                "reason": f"coupon only supports {supported}; {arch!r} is out of scope for v1"}

    notes: list[str] = []
    ops: list[dict[str, Any]] = []

    if arch == "shaft_in_bore":
        dia = _dim_value(feat.dims, "dia") or 5.0
        stack = process_profiles.modeled_dim(float(dia), _fit_for_math(feat.fit, proc), proc,
                                             is_hole=True)
        bore = stack["modeled_mm"]
        plate = max(bore + 8.0, 16.0)
        ops = [
            {"op": "box", "name": "coupon", "x_mm": plate, "y_mm": plate, "z_mm": 6.0},
            {"op": "hole", "name": "test_bore", "face": "top face",
             "dia_mm": round(bore, 3), "depth": "through", "at": [0, 0]},
        ]
        notes.append(f"bore stacked to Ø{bore:g} mm ({stack['rationale']})")

    elif arch == "bolt_pattern":
        spacing = _dim_value(feat.dims, "spacing") or 18.0
        hole_d = _dim_value(feat.dims, "dia") or 2.2
        count = int(feat.dims.get("count", feat.dims.get("count_n", 2)) or 2)
        # positional fit: holes go on spacing with NO diametral clearance added (§1 step 9)
        plate_x = spacing + 16.0
        plate_y = 24.0
        ops = [
            {"op": "box", "name": "coupon", "x_mm": plate_x, "y_mm": plate_y, "z_mm": 6.0},
        ]
        half = spacing / 2.0
        xs = [(-half + i * spacing / max(count - 1, 1)) for i in range(count)] if count > 1 \
            else [0.0]
        for i, x in enumerate(xs):
            ops.append({"op": "hole", "name": f"mount_{i + 1}", "face": "top face",
                        "dia_mm": round(float(hole_d), 3), "depth": "through", "at": [round(x, 3), 0]})
        notes.append(f"{count} holes at {spacing:g} mm spacing, positional (no diametral clearance)")

    else:  # connector_cutout
        w = _dim_value(feat.dims, "width") or 9.0
        h = _dim_value(feat.dims, "height") or 3.5
        clr = process_profiles.fit_clearance(proc, _fit_for_math(feat.fit, proc)) \
            if process_profiles.PROFILES[proc].fit_classes_mm.get(
                _fit_for_math(feat.fit, proc)) is not None else 0.0
        cut_w = round(w + clr, 3)
        cut_h = round(h + clr, 3)
        plate_x = max(w + 14.0, 20.0)
        ops = [
            {"op": "box", "name": "coupon", "x_mm": plate_x, "y_mm": 16.0, "z_mm": 6.0},
            {"op": "rect_cut", "name": "connector_opening", "face": "front face",
             "width_mm": cut_w, "height_mm": cut_h, "depth": "through", "at": [0, 0]},
        ]
        notes.append(f"opening {cut_w:g}x{cut_h:g} mm (nominal {w:g}x{h:g} + {clr:g} clearance)")

    return {
        "ok": True,
        "archetype": arch,
        "interface": feat.key,
        "process": proc,
        "est_print_min": 20,
        "est_mass_g": 5,
        "ops": ops,
        "notes": notes,
    }


# ===========================================================================================
# 7. NEVER-BLOCK POLICY — the §3 default: one ask, proceed with an at_risk warning; hard-block
#    only on a material impossibility or (strict_fit and a press/bearing fit on an unmeasured bore).
# ===========================================================================================

# fit classes whose unmeasured bore is a hard stop under strict-fit (the §3 narrow must-block list)
_STRICT_BLOCK_FITS: frozenset[str] = frozenset({"press", "interference", "bearing", "slip"})


def gate_decision(ledger: CriticalDimensionLedger, strict_fit: bool = False, *,
                  process: Optional[str] = None,
                  material: Optional[str] = None) -> dict[str, Any]:
    """Decide whether to block the run, what to ask, and what to warn (plan §3 never-block policy).

    **Default = never hard-block.** An unresolved critical dim produces **one clear ask** and the
    run proceeds with a recorded low-confidence assumption and a loud ``at_risk`` warning
    (preserving clarify's never-block ethos). The only hard stops (the §3 narrow MUST-BLOCK list):

    * **Material impossible for the process** — metal named for an FDM build (a typed material that
      the process can't make). Passed via ``material`` + ``process``.
    * **strict_fit AND a press/bearing/slip fit resting on an unmeasured bore** — the opt-in expert
      guarantee, off by default.

    Returns ``{block, ask, warnings, must_block_reasons}`` where ``ask`` is the (≤ a few)
    prioritized asks to surface and ``warnings`` are the FitReport-bound at_risk notes.
    """
    proc = _norm_process(process or ledger.process)
    unresolved = unresolved_critical(ledger)

    warnings: list[str] = []
    asks: list[dict[str, Any]] = []
    must_block_reasons: list[str] = []

    # ---- hard stop 1: material impossible for the process (always blocks) ----------
    if material is not None and _material_impossible(material, proc):
        must_block_reasons.append(
            f"material {material!r} is impossible for process {proc!r} "
            f"(e.g. metal cannot be FDM-printed)")

    # ---- hard stop 2 (opt-in): strict_fit on an unmeasured tight bore --------------
    for row in unresolved:
        fc = str(row.fit or "").strip().lower()
        if strict_fit and fc in _STRICT_BLOCK_FITS:
            must_block_reasons.append(
                f"strict-fit: {row.key} is a {fc} fit resting on an unmeasured bore")

    # ---- the single prioritized ask (never-block default) --------------------------
    for row in unresolved:
        asks.append({
            "kind": "dimension_confirm",
            "key": row.key,
            "feature": row.feature,
            "unit": "mm",
            "ask": _demand_ask(row, row.key.split(".", 1)[-1], row.required_tier),
            "suggested": row.suggested if row.suggested is not None else row.nominal_mm,
            "why_it_matters": row.why_it_matters,
        })
        warnings.append(
            f"{row.key} unresolved — proceeding with a default; fit is AT RISK "
            f"({row.why_it_matters}).")

    block = bool(must_block_reasons)
    return {
        "block": block,
        # on a never-block proceed we still surface ONE ask (the plan's "one clear ask")
        "ask": asks,
        "warnings": warnings,
        "must_block_reasons": must_block_reasons,
    }


def _material_impossible(material: str, process: str) -> bool:
    """True if a named material cannot be made by the process (the only material hard stop today)."""
    m = str(material or "").strip().lower()
    proc = _norm_process(process)
    metals = ("steel", "stainless", "aluminum", "aluminium", "titanium", "brass", "bronze",
              "copper", "iron", "metal", "6061")
    is_metal = any(w in m for w in metals)
    # FDM/injection (the polymer processes) cannot make a metal part
    if is_metal and proc in ("fdm", "injection"):
        return True
    return False


# ===========================================================================================
# coercion helpers — accept dataclasses, their dicts, or bare feature lists everywhere
# ===========================================================================================


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _norm_process(process: Any) -> str:
    p = _norm(process)
    return p if p in process_profiles.PROFILES else process_profiles.DEFAULT_PROCESS


def _coerce_feature(f: Any) -> Optional[MeasuredFeature]:
    """Accept a MeasuredFeature, its dict, or a minimal ``{key,archetype,fit}`` dict."""
    if isinstance(f, MeasuredFeature):
        return f
    if isinstance(f, dict) and f.get("key"):
        return MeasuredFeature.from_dict({
            "key": f["key"],
            "archetype": f.get("archetype", "unknown"),
            "dims": f.get("dims") or {k: v for k, v in f.items()
                                       if k.endswith("_mm") or k in ("count", "spacing", "dia",
                                                                     "x", "y", "width", "height")},
            "fit": f.get("fit") or (ARCHETYPES[f["archetype"]].default_fit
                                    if f.get("archetype") in ARCHETYPES else "clearance"),
            "confidence": f.get("confidence", "low"),
            "uncert_mm": f.get("uncert_mm"),
            "provenance": f.get("provenance") or {},
        })
    return None


def _coerce_features(measured: Any) -> list[MeasuredFeature]:
    """Pull a feature list out of a part, a part-dict, or a bare list."""
    if isinstance(measured, MeasuredPartProfile):
        return list(measured.features)
    if isinstance(measured, dict) and "features" in measured:
        return [MeasuredFeature.from_dict(f) for f in (measured.get("features") or [])]
    if isinstance(measured, (list, tuple)):
        out: list[MeasuredFeature] = []
        for f in measured:
            cf = _coerce_feature(f)
            if cf is not None:
                out.append(cf)
        return out
    return []


def _coerce_part(measured: Any) -> Optional[MeasuredPartProfile]:
    """Accept a MeasuredPartProfile, its dict, or a bare feature list -> a part (or None)."""
    if isinstance(measured, MeasuredPartProfile):
        return measured
    if isinstance(measured, dict) and ("features" in measured or "id" in measured):
        return MeasuredPartProfile.from_dict({
            "id": measured.get("id", "mp_x"),
            "label": measured.get("label", ""),
            "source": measured.get("source", "photo"),
            "images": measured.get("images") or [],
            "envelope": measured.get("envelope") or {},
            "features": measured.get("features") or [],
            "datum": measured.get("datum") or {},
        })
    if isinstance(measured, (list, tuple)):
        return MeasuredPartProfile(id="mp_x", label="", features=_coerce_features(measured))
    return None
