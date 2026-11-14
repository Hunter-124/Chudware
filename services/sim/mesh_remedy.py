"""Rephrase Polyhedral-FEA's CLI remedies in the FEA tools' own parameter names.

The in-process mesher (core/sim/mesher.cpp) passes polymesh's refusals through verbatim
apart from the unit relabel ``in_model_units`` applies. Those refusals were written for the
polymesh CLI: they say ``Reduce -h to <= 5 mm``, ``raise --max-elems/--max-dof`` and
``Retry with the default --mesher hybrid``. None of that is an argument an agent can pass:
the FEA tools take ``mesh_size_mm`` / ``min_size_mm`` / ``mesh_fineness``, mesh with the
graded-tet mesher only, and derive the element/DOF ceilings from the machine's memory
budget. The numbers are kept; only the remedy wording is translated.

Units: Chudware hands polymesh the STEP in model millimetres with no ``--scale``, so every
length in an engine-relayed refusal is millimetres whether it is labelled ``m`` or ``mm``
(see ``in_model_units``). The standalone ``polymesh_cli`` tool is a different path and its
output is not rewritten here.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Union

#: Appended once to a rewritten message; also the idempotence marker.
MARKER = "MCP remedy:"

_NUM = r"([0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?)"
_UNIT = r" ?m{1,2}\b"

_REWRITES: tuple[tuple[re.Pattern, Union[str, Callable[[re.Match], str]]], ...] = (
    (re.compile(r"requested -h " + _NUM + _UNIT), r"requested element size \1 mm"),
    (re.compile(r"([Rr])educe -h to <= " + _NUM + _UNIT),
     lambda m: ("S" if m.group(1) == "R" else "s") + f"et mesh_size_mm <= {m.group(2)}"),
    (re.compile(r"([Rr])educing -h a little"), r"\1educing mesh_size_mm a little"),
    (re.compile(r",? *\(?or raise --max-elems/--max-dof(?: to afford it)?\)?"), ""),
    (re.compile(r"increase -h or raise --max-(?:elems|dof)"), "increase mesh_size_mm"),
    (re.compile(r"([Rr])educe -h\b"), r"\1educe mesh_size_mm"),
    (re.compile(r"Retry with the default --mesher hybrid, which is watertight on every "
                r"part measured"),
     "The FEA tools always mesh with the graded-tet mesher (there is no mesher "
     "argument), so retry with a different mesh_size_mm, or check and repair the "
     "geometry where bodies touch or overlap (check_geometry_health, heal_body)"),
    (re.compile(r"raise fineness or set min_size_mm"),
     "raise mesh_fineness (or lower mesh_size_mm) or set min_size_mm"),
)

_SUGGESTED = re.compile(r"[Ss]et mesh_size_mm <= " + _NUM)

_TAIL = (
    " " + MARKER + " sizes in this message are millimetres, as are mesh_size_mm and "
    "min_size_mm (only the standalone polymesh CLI's -h is in metres). The element/DOF "
    "ceilings come from this machine's memory budget and are not tool arguments, and "
    "mesh_size_mm cannot be combined with a non-default mesh_fineness. A retry can still "
    "be under-resolved: check mesh_quality and convergence before relying on it."
)


def to_mcp(text: str) -> str:
    """`text` with polymesh CLI remedies rewritten; unchanged when it carries none."""
    if not isinstance(text, str) or MARKER in text:
        return text
    out = text
    for pattern, replacement in _REWRITES:
        out = pattern.sub(replacement, out)
    if out == text:
        return text
    suggested = [float(m) for m in _SUGGESTED.findall(out)]
    tail = _TAIL
    if suggested:
        tail += f" Suggested retry: mesh_size_mm={min(suggested):.4g}."
    return out + tail


def apply(value: Any) -> Any:
    """:func:`to_mcp` over every string in a JSON-shaped value."""
    if isinstance(value, str):
        return to_mcp(value)
    if isinstance(value, dict):
        return {k: apply(v) for k, v in value.items()}
    if isinstance(value, list):
        return [apply(v) for v in value]
    return value
