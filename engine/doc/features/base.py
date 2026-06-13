"""Feature-compiler registry + per-feature compile context.

A feature compiler is a function ``compile_<type>(feat, ctx) -> CompileOutput`` registered
under its feature-type string via the :func:`register` decorator. The compiler core
(``engine/doc/compiler.py``) looks the function up by ``feature["type"]`` and calls it with the
feature dict and a :class:`CompileContext` carrying the resolved equation env, the bodies built
so far, and the datum table (reserved datums per doc_schema.md §5 plus any datum features built
upstream).

Dimension resolution: every ``dim`` value object ``{"value": float, "expr": str|null}`` is
resolved against the equation env BEFORE a compiler runs (the compiler core walks params and
overwrites ``value`` with the evaluated expr when ``expr != null``). :meth:`CompileContext.dim`
is the accessor a compiler uses to read a resolved float for a param key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- registry

# feature-type string -> compiler callable(feat: dict, ctx: CompileContext) -> CompileOutput
_REGISTRY: dict[str, "FeatureCompiler"] = {}

FeatureCompiler = Callable[[dict, "CompileContext"], "CompileOutput"]


def register(ftype: str) -> Callable[[FeatureCompiler], FeatureCompiler]:
    """Decorator: register a feature compiler under its feature-type string."""

    def deco(fn: FeatureCompiler) -> FeatureCompiler:
        if ftype in _REGISTRY:
            raise ValueError(f"feature compiler already registered for '{ftype}'")
        _REGISTRY[ftype] = fn
        return fn

    return deco


def get_compiler(ftype: str) -> FeatureCompiler | None:
    return _REGISTRY.get(ftype)


def registered_types() -> set[str]:
    return set(_REGISTRY)


# --------------------------------------------------------------------------- datum

@dataclass
class Datum:
    """A datum entity stored in the context (never a body).

    ``kind`` is one of ``plane`` | ``axis`` | ``point`` | ``csys``. ``obj`` holds the
    build123d carrier (``Plane`` / ``Axis`` / ``Vector`` / ``Location``). ``feature_id`` is the
    owning feature id (or the reserved id for built-ins).
    """

    kind: str
    obj: Any
    feature_id: str


# --------------------------------------------------------------------------- compile output

class FeatureError(Exception):
    """Raised by a feature compiler to signal a contained, recoverable failure.

    The compiler core catches this, records ``ok:false`` with the message (+ optional hint),
    and continues the history on the last good body. ``dangling_refs`` lists ref-slot locators
    that failed to resolve (selectors land in C03/C09; primitives/datums here rarely set it).
    """

    def __init__(self, message: str, *, hint: str | None = None,
                 dangling_refs: list[str] | None = None) -> None:
        super().__init__(message)
        self.hint = hint
        self.dangling_refs = dangling_refs or []

    def full_message(self) -> str:
        return self.args[0] + ("\n\nHINT: " + self.hint if self.hint else "")


@dataclass
class CompileOutput:
    """What a feature compiler returns to the core.

    - ``bodies``: the FULL body list after this feature (the compiler receives the prior bodies
      via the context and returns the new list — most features replace the last body, primitives
      with ``merge:false`` append a new one, datums return the bodies unchanged).
    - ``tool_body``: the solid this feature fused or subtracted (patterns/mirror need it later);
      None for datums/scripts that don't contribute a single tool solid.
    - ``op``: the boolean this feature applied with its ``tool_body`` — ``"add"`` (fused) or
      ``"cut"`` (subtracted). Patterns/mirror read it to replicate an instance with the SAME
      boolean. ``None`` defaults to ``"add"`` in the core (primitives, bosses).
    - ``datum``: a :class:`Datum` to register (datum features only).
    - ``resolved``: resolved dim values to echo back in feature_status (``{param: float}``).
    """

    bodies: list[Any] = field(default_factory=list)
    tool_body: Any | None = None
    op: str | None = None
    datum: Datum | None = None
    resolved: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- context

@dataclass
class CompileContext:
    """Per-feature compilation context.

    ``equations_env`` is the once-evaluated global equation environment. ``bodies`` is the live
    list of bodies built so far (compilers read the last one as their working body). ``datums``
    maps datum id -> :class:`Datum` (reserved built-ins pre-seeded by the core). ``_resolved``
    holds the current feature's params with every dim already evaluated to a plain float in its
    ``value`` slot — :meth:`dim` reads from it.
    """

    equations_env: dict[str, float]
    bodies: list[Any]
    datums: dict[str, Datum]
    # Per-feature tool solids built SO FAR (feature_id -> shape) and the boolean each applied
    # (feature_id -> "add"|"cut"). The compiler core threads the live accumulating dicts in, so a
    # pattern/mirror feature can look up the solids of the features it replicates. Empty for the
    # leading features; populated by the time a pattern/mirror runs (it always references upstream).
    tool_bodies: dict[str, Any] = field(default_factory=dict)
    feature_ops: dict[str, str] = field(default_factory=dict)
    _resolved_params: dict[str, Any] = field(default_factory=dict)

    # -- dim access -------------------------------------------------------

    def dim(self, params: dict, key: str, default: float | None = None) -> float:
        """Return the resolved float for a dimension-or-number param ``key``.

        Accepts a dim object ``{value, expr}`` (value already evaluated by the core), a bare
        number, or absence (returns ``default``). Raises :class:`FeatureError` if the key is
        missing and no default is given, or the value isn't numeric.
        """

        if key not in params:
            if default is not None:
                return float(default)
            raise FeatureError(f"missing required dimension '{key}'")
        v = params[key]
        if isinstance(v, dict):
            val = v.get("value")
        else:
            val = v
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise FeatureError(f"dimension '{key}' is not numeric: {v!r}")
        return float(val)

    # -- datum access -----------------------------------------------------

    def datum(self, ref: Any) -> Datum:
        """Resolve a selector-or-datum ref into a :class:`Datum`.

        Accepts a bare reserved/feature id string, or a ``{"kind":"datum","feature":id}``
        selector. Geometry selectors (face/edge/vertex) are not resolvable here — that lands in
        C03/C09; for now they raise :class:`FeatureError` (dangling).
        """

        fid: str | None = None
        if isinstance(ref, str):
            fid = ref
        elif isinstance(ref, dict) and ref.get("kind") == "datum":
            fid = ref.get("feature")
        if fid is None:
            raise FeatureError(
                f"cannot resolve '{ref!r}' to a datum here (geometry selectors resolve in C03/C09)")
        d = self.datums.get(fid)
        if d is None:
            raise FeatureError(f"unknown datum '{fid}'")
        return d
