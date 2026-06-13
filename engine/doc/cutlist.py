"""Weldment cut list — aggregate ``weld_member`` structural members into a cut-list table.

The signature weldment deliverable: every structural member produced by the ``weld_member`` feature
(``engine/doc/features/weldment.py``) is grouped by ``(profile + its resolved dims + length)`` so
identical members collapse into ONE row carrying a ``quantity`` and the running item numbers — the
SolidWorks "Weldment Cut List".

Source data
-----------
The ``weld_member`` compiler stashes, per feature id, ``{profile, dims, members:[{segment_id,
profile, length, start, end, area, volume}]}`` on the ``ctx.datums["_weld_members"]`` sentinel; the
compiler surfaces that dict onto ``CompileResult.weld_members`` (see ``engine/doc/compiler.py``).
:func:`build_cut_list` is a PURE function over that dict — it takes the ``weld_members`` mapping and
returns the cut-list rows, so it is unit-testable without the worker or a live session.

Grouping
--------
Two members are the same line item iff they share the same profile name, the same resolved
dimensions, AND the same length rounded to ``_LEN_ROUND`` (0.1 mm). Each group becomes one row:

    {item, profile, description, length_mm, quantity, total_length_mm}

``item`` is the 1-based row number after sorting (profile name asc, then length desc).
``description`` is a human label, e.g. ``"Square Tube 20 x 20 x 2"`` (see :func:`_describe`).

Canonical units: lengths/dims are millimetres throughout.
"""

from __future__ import annotations

from typing import Any

# Group/round member lengths to this resolution (mm) so float jitter in the swept-edge length does
# not split two nominally-identical members into separate rows.
_LEN_ROUND = 1  # decimal places => 0.1 mm


# --------------------------------------------------------------------------- human descriptions


# Per-profile description templates. Each entry: (label, [dim keys in display order]). A profile not
# listed falls back to "<Title Case name> <sorted dims>" so a new library profile still reads sanely.
_PROFILE_LABELS: dict[str, tuple[str, list[str]]] = {
    "square_tube": ("Square Tube", ["size", "size", "thickness"]),
    "rect_tube": ("Rectangular Tube", ["w", "h", "thickness"]),
    "round_tube": ("Round Tube", ["od", "thickness"]),
    "angle_L": ("Angle", ["leg", "leg", "thickness"]),
    "flat_bar": ("Flat Bar", ["w", "thickness"]),
    "c_channel": ("C-Channel", ["h", "w", "thickness"]),
}


def _fmt_num(v: float) -> str:
    """Format a dimension: drop a trailing ``.0`` so ``20.0`` reads ``20`` but ``2.5`` stays."""
    f = float(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:g}"


def _describe(profile: str, dims: dict[str, Any]) -> str:
    """Human profile label, e.g. ``square_tube {size:20,thickness:2}`` -> ``"Square Tube 20 x 20 x 2"``.

    Unknown profiles fall back to a title-cased name plus the dims in sorted-key order so a freshly
    added library profile still produces a readable description without touching this table.
    """
    spec = _PROFILE_LABELS.get(profile)
    if spec is not None:
        label, keys = spec
        parts = [_fmt_num(dims[k]) for k in keys if k in dims]
        return (label + " " + " x ".join(parts)).strip() if parts else label
    # Fallback: "Square Tube"-ish title case of the raw name + its dims in stable key order.
    title = " ".join(w.capitalize() for w in profile.replace("_", " ").split())
    parts = [_fmt_num(dims[k]) for k in sorted(dims)]
    return (title + " " + " x ".join(parts)).strip() if parts else title


# --------------------------------------------------------------------------- builder


def _dims_key(dims: dict[str, Any]) -> tuple:
    """A hashable, order-independent key for a resolved-dims dict (rounded to avoid float jitter)."""
    return tuple(sorted((k, round(float(v), 4)) for k, v in (dims or {}).items()))


def build_cut_list(weld_members: dict[str, Any]) -> dict[str, Any]:
    """Build the cut-list table from a ``CompileResult.weld_members`` mapping (PURE function).

    ``weld_members`` is ``{feature_id: {profile, dims, members:[{…, length, …}]}}``. Members across
    ALL weld_member features are pooled and grouped by ``(profile, dims, length@0.1mm)``. Returns::

        {rows:[{item, profile, description, length_mm, quantity, total_length_mm}],
         total_members, total_length_mm, profiles:[…]}

    Rows are sorted by profile (asc) then length (desc); ``item`` is the 1-based post-sort index.
    An empty/None mapping yields empty rows + zero totals (NOT an error) — a part with no weldment
    members simply has nothing to cut.
    """
    # group key -> accumulator {profile, dims, description, length_mm, quantity, total_length_mm}
    groups: dict[tuple, dict[str, Any]] = {}
    total_members = 0
    total_length = 0.0
    profiles_seen: set[str] = set()

    for _fid, rec in (weld_members or {}).items():
        if not isinstance(rec, dict):
            continue
        profile = rec.get("profile", "?")
        dims = rec.get("dims", {}) or {}
        for m in rec.get("members", []) or []:
            if not isinstance(m, dict):
                continue
            # Each member's own profile wins if present (one feature = one profile today, but be
            # defensive); dims are the feature-level resolved dims.
            mp = m.get("profile", profile)
            length = round(float(m.get("length", 0.0) or 0.0), _LEN_ROUND)
            key = (mp, _dims_key(dims), length)
            g = groups.get(key)
            if g is None:
                g = {
                    "profile": mp,
                    "description": _describe(mp, dims),
                    "length_mm": length,
                    "quantity": 0,
                    "total_length_mm": 0.0,
                }
                groups[key] = g
            g["quantity"] += 1
            g["total_length_mm"] = round(g["total_length_mm"] + length, _LEN_ROUND)
            total_members += 1
            total_length += length
            profiles_seen.add(mp)

    # Sort: profile name ascending, then length descending (longest cut first within a profile).
    rows = sorted(groups.values(),
                  key=lambda r: (r["profile"], -r["length_mm"]))
    for i, r in enumerate(rows, start=1):
        r["item"] = i

    return {
        "rows": rows,
        "total_members": total_members,
        "total_length_mm": round(total_length, _LEN_ROUND),
        "profiles": sorted(profiles_seen),
    }


# --------------------------------------------------------------------------- session entry point


def cut_list_for_session(session) -> dict[str, Any]:
    """Build the cut list for a live :class:`engine.doc.session.DocSession`.

    Forces a fresh (uncached) compile so the ``weld_member`` compiler is guaranteed to run and
    repopulate the ``_weld_members`` side-channel (a CACHE-HIT weld_member would leave it empty,
    exactly like the ``_sketch_regions`` cache caveat documented in ``session.py``). Assemblies have
    no part-feature weldment topology, so they report an empty cut list.
    """
    from engine.doc.compiler import compile_document, CompileCache

    if getattr(session, "is_assembly", False):
        return build_cut_list({})

    # Compile against a FRESH cache so weld_member actually runs (vs. a hit that skips the
    # side-channel write). This is read-only — we never touch session.last_result / the live cache.
    result = compile_document(session.doc, CompileCache())
    return build_cut_list(result.weld_members)
