"""Template registry + the shipped templates (contract §8).

A **template** is a named recipe that maps outline blocks to a file via one engine, and — the
load-bearing bit — declares which slot names / workbook columns are *load-bearing* per block
type (contract §4.3), so the validator can reject a literal where a provenance ref is required.
Templates are looked up by name through a generic registry, so real engineering templates slot
in with **no contract change** (contract §8).

Shipped here:

- ``deliverables/1`` smoke templates (:mod:`.smoke`): ``smoke_deck`` / ``smoke_report`` /
  ``smoke_workbook`` — one per format, proving the seam end-to-end.
- **Engineering templates** — the standard documents a professional engineer ships, every
  load-bearing number resolved from an attached tool result (contract §4):
  ``design_review`` (pptx), ``calc_package`` (docx/pdf), ``bom_workbook`` (xlsx),
  ``verification_report`` (docx/pdf), ``assembly_manual`` (docx/pdf),
  ``work_instruction`` (docx/pdf), and ``weldment_traveler`` (docx/pdf).

Every engine import is lazy (inside a builder) so importing this package — e.g. for
``list_artifacts`` — never requires python-pptx/docx/openpyxl/reportlab to be installed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .. import project
from ..errors import DeliverableError


class Template:
    """A named recipe. ``build(resolved, fmt, out_path)`` writes the file with one engine.

    ``lb_slots`` maps a block type to the set of slot names that are load-bearing (a literal
    there is a ``provenance_violation``); ``lb_cols`` does the same for workbook/table column
    indices; ``lb_all`` is the set of block types where **every** slot is load-bearing (used by
    calc equation blocks, where all slots are computed engineering values).
    """

    def __init__(self, name: str, kind: str,
                 build: Callable[[dict, str, Path], None],
                 lb_slots: dict[str, set] | None = None,
                 lb_cols: dict[str, set] | None = None,
                 lb_all: set[str] | None = None) -> None:
        self.name = name
        self.kind = kind
        self._build = build
        self._lb_slots = lb_slots or {}
        self._lb_cols = lb_cols or {}
        self._lb_all = set(lb_all or ())

    def load_bearing_slots(self, block_type: str) -> set:
        return self._lb_slots.get(block_type, set())

    def load_bearing_columns(self, block_type: str) -> set:
        return self._lb_cols.get(block_type, set())

    def all_slots_load_bearing(self, block_type: str) -> bool:
        return block_type in self._lb_all

    def build(self, resolved: dict, fmt: str, out_path: Path) -> None:
        self._build(resolved, fmt, out_path)


_REGISTRY: dict[str, Template] = {}


def register_template(t: Template) -> None:
    _REGISTRY[t.name] = t


def get_template(name: str, kind: str) -> Template:
    t = _REGISTRY.get(name)
    if t is None:
        raise DeliverableError("invalid_spec", f"unknown template {name!r}")
    if t.kind != kind:
        raise DeliverableError(
            "invalid_spec", f"template {name!r} builds {t.kind!r}, not {kind!r}")
    return t


def registered_templates() -> dict[str, Template]:
    """Snapshot of the registry (name → Template) — for guide/introspection."""
    return dict(_REGISTRY)


# --------------------------------------------------------------------------- shared helpers


def slot_text(block: dict, name: str, default: str = "") -> str:
    """The resolved display text of one slot (the number the engine gave), or ``default``."""
    rv = block.get("slots", {}).get(name)
    return rv["text"] if rv else default


def slot_lines(block: dict) -> list[str]:
    """Human ``Label: value`` lines for a block's resolved slots (numbers from tool results)."""
    out = []
    for name, rv in block.get("slots", {}).items():
        label = name.replace("_", " ").strip().capitalize()
        out.append(f"{label}: {rv['text']}")
    return out


# Back-compat alias for the smoke templates (moved from the old flat templates.py).
_slot_lines = slot_lines


def resolve_image(path) -> Path | None:
    """A project-relative/absolute image path that actually exists on disk, else ``None``.

    Image blocks (design-review views, assembly step renders) name a render PNG; embedding is
    best-effort — a missing file degrades to a caption, never an error (the numbers, not the
    pictures, are the load-bearing part).
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        p = project.resolve_project_path(path)
    except Exception:  # noqa: BLE001
        return None
    return p if (p.exists() and p.is_file()) else None


# --------------------------------------------------------------------------- registration

# Importing each submodule registers its templates by side effect. Kept at the bottom so the
# names above (Template, register_template, the helpers) already exist when the submodules import
# them from this package.
from . import (  # noqa: E402,F401
    assembly_manual,
    bom_workbook,
    calc_package,
    design_review,
    smoke,
    user_manual,
    verification_report,
    weldment_traveler,
    work_instruction,
)

__all__ = [
    "Template",
    "register_template",
    "get_template",
    "registered_templates",
    "slot_text",
    "slot_lines",
    "resolve_image",
]
