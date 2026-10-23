"""Parse clean-room BOLTS-format ``.blt.yaml`` standard-part tables.

This loader reads the small, documented YAML subset under ``services/parts/data/bolts/``
(see that folder's ``README.md``) and exposes it as ``{standard_id: Standard}``, where each
:class:`Standard` maps a size designation to its nominal dimensions **and** to a
``core/parts`` catalog family so the spec-sheet matcher can build real geometry.

The bundled data is clean-room ISO/DIN dimensional data (NOT the BOLTS project's GPL data —
see ``contracts/licenses.md``). The parser accepts the
BOLTS ``.blt`` *concept* (collection → standards → size table), so a user who points
``load_bolts_tables`` at their own installed BOLTS checkout is supported by the same code
without us redistributing GPL data.

No network, no side effects: importing/loading only reads bundled YAML files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

# Bundled data directory (clean-room tables ship here).
DATA_DIR = Path(__file__).resolve().parent / "data" / "bolts"


@dataclass(frozen=True)
class Standard:
    """One governing standard (e.g. ISO 4762) and its size table.

    ``sizes`` maps a designation (``"M3"``, ``"608"``) to a plain dict of nominal
    parameters in millimetres. ``family`` is the ``core/parts`` catalog family key that
    ``parts_make`` accepts; ``needs_length`` mirrors that catalog's per-family flag.
    """

    id: str
    name: str
    family: str
    kind: str = "fastener"           # "fastener" | "bearing"
    needs_length: bool = False
    aliases: tuple[str, ...] = ()
    sizes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, size: str) -> dict[str, Any] | None:
        """Return the param dict for ``size`` (case/format tolerant), or ``None``."""
        if size in self.sizes:
            return self.sizes[size]
        want = _norm_size_key(size)
        for k, v in self.sizes.items():
            if _norm_size_key(k) == want:
                return v
        return None


def _norm_size_key(size: str) -> str:
    return str(size).strip().upper().replace(" ", "")


def _coerce_standard(raw: dict[str, Any]) -> Standard | None:
    """Build a :class:`Standard` from one parsed YAML mapping, or ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("id") or "").strip()
    family = str(raw.get("family") or "").strip()
    if not sid or not family:
        return None
    sizes_raw = raw.get("sizes") or {}
    sizes: dict[str, dict[str, Any]] = {}
    if isinstance(sizes_raw, dict):
        for key, params in sizes_raw.items():
            if isinstance(params, dict):
                sizes[str(key)] = {str(k): v for k, v in params.items()}
    aliases = tuple(str(a) for a in (raw.get("aliases") or []) if str(a).strip())
    return Standard(
        id=sid,
        name=str(raw.get("name") or sid),
        family=family,
        kind=str(raw.get("kind") or "fastener"),
        needs_length=bool(raw.get("needs_length", False)),
        aliases=aliases,
        sizes=sizes,
    )


def _iter_blt_files(data_dir: Path) -> Iterable[Path]:
    for pat in ("*.blt.yaml", "*.blt.yml", "*.blt"):
        yield from sorted(data_dir.glob(pat))


def load_bolts_tables(data_dir: str | Path | None = None) -> dict[str, Standard]:
    """Load every ``.blt`` table under ``data_dir`` (default: bundled ``data/bolts/``).

    Returns ``{standard_id: Standard}``. Later files override earlier standards with the same
    ``id``. Missing directory → empty dict (never raises); a malformed file is skipped, not
    fatal, so one bad table can't take down the whole lookup.
    """
    root = Path(data_dir).expanduser() if data_dir else DATA_DIR
    out: dict[str, Standard] = {}
    if not root.exists():
        return out
    for path in _iter_blt_files(root):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a broken table must not sink the rest
            continue
        if not isinstance(doc, dict):
            continue
        for raw in doc.get("standards") or []:
            std = _coerce_standard(raw)
            if std is not None:
                out[std.id] = std
    return out


def designation_index(tables: dict[str, Standard] | None = None) -> dict[str, Standard]:
    """Map every standard id + alias (normalized) to its :class:`Standard` for fast lookup."""
    tables = load_bolts_tables() if tables is None else tables
    idx: dict[str, Standard] = {}
    for std in tables.values():
        for name in (std.id, *std.aliases):
            idx[_norm_designation(name)] = std
    return idx


def _norm_designation(name: str) -> str:
    return str(name).strip().upper().replace("-", " ").replace("_", " ")
