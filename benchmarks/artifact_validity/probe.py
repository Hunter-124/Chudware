"""The independent artifact probe: a FRESH engine, reopening what the turn left on disk.

This is the half of the benchmark the turn cannot influence. It never reads the turn's
transcript or tool ledger — it reopens files and asks the engine.

Two directories are probed per prompt:
  <run>/work      the turn's workspace (the deliverables the model chose to save)
  <run>/autosave  CHUDWARE_AUTOSAVE_DIR, where services/mcp/cad_common.py::_autosave
                  snapshots EVERY doc the turn touched, whether or not the model
                  remembered to save it. Without this, "the model built a correct part
                  but forgot to save" would score identically to "the model built
                  nothing", and those are very different defects.

Requires the engine. Import is lazy so scoring.py and its tests stay engine-free.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: Openable part containers, in the order we prefer them.
PART_EXTS = (".step+", ".step", ".stp", ".brep", ".stl", ".3mf")

_PDF_PAGE = re.compile(rb"/Type\s*/Page[^s]")


def pdf_pages(path: Path) -> int | None:
    """Page count straight off the PDF bytes — no third-party parser, so a missing
    dependency can never turn a real PDF into an unscored prompt."""
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    n = len(_PDF_PAGE.findall(blob))
    if n:
        return n
    m = re.search(rb"/Count\s+(\d+)", blob)
    return int(m.group(1)) if m else None


def _fact(p: Path) -> dict:
    fact = {"path": str(p), "name": p.name, "bytes": p.stat().st_size, "pages": None}
    if p.suffix.lower() == ".pdf":
        fact["pages"] = pdf_pages(p)
    return fact


def file_facts(dirs: list[Path]) -> list[dict]:
    """Every regular file the turn produced, with its size (and page count for PDFs)."""
    out: list[dict] = []
    seen: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            key = p.name + ":" + str(p.stat().st_size)
            if key in seen:
                continue
            seen.add(key)
            out.append(_fact(p))
    return out


#: Keys under which an export tool reports where it actually wrote.
_PATH_KEYS = ("path", "output_path", "out_path", "saved_path", "export_path",
              "pdf_path", "dxf_path", "step_path", "stl_path", "file", "filename")


def reported_files(record: dict) -> list[dict]:
    """Files a SUCCESSFUL tool said it wrote, wherever it wrote them.

    Needed because export_drawing defaults to a temp path when the caller does not name
    one (observed: /tmp/chudware_mcp_*.pdf). Scoring "did a real PDF get produced" must not
    silently become "did the model remember to pass an output path" — that is a different
    question, and conflating them would make the criterion measure the wrong thing."""
    from .scoring import deep_values, payload  # noqa: PLC0415 — pure module, no engine

    out: list[dict] = []
    seen: set[str] = set()
    for c in record.get("calls") or []:
        if not c.get("ok"):
            continue
        for _k, v in deep_values(payload(c), _PATH_KEYS):
            if not isinstance(v, str) or not v.startswith("/"):
                continue
            p = Path(v)
            if str(p) in seen or not p.is_file():
                continue
            seen.add(str(p))
            out.append(_fact(p))
    return out


def _health(cb, tools, doc_id: str) -> dict:
    raw = tools["check_geometry_health"].fn(doc_id=doc_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _open(cb, path: Path) -> str | None:
    """doc_open for the native container, import_file for interchange formats."""
    try:
        r = cb.call("doc_open", {"path": str(path)})
        if isinstance(r, dict) and r.get("doc_id"):
            return str(r["doc_id"])
    except Exception:  # noqa: BLE001 — a file we cannot open is a probe miss, not a crash
        pass
    try:
        r = cb.call("import_file", {"path": str(path)})
        if isinstance(r, dict) and r.get("doc_id"):
            return str(r["doc_id"])
    except Exception:  # noqa: BLE001
        pass
    return None


def part_facts(dirs: list[Path]) -> list[dict]:
    """Reopen every part container and record ONLY engine-computed facts."""
    from services.mcp import core_bridge as cb  # noqa: PLC0415 — engine, lazily
    from services.mcp import server  # noqa: PLC0415

    tools = server.mcp._tool_manager._tools
    candidates: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.name.lower().endswith(PART_EXTS):
                candidates.append(p)

    out: list[dict] = []
    for p in candidates:
        doc = _open(cb, p)
        if not doc:
            out.append({"path": str(p), "opened": False, "watertight": False,
                        "free_edges": None, "non_manifold_edges": None,
                        "volume_mm3": 0.0, "bbox_size_mm": None, "bodies": 0,
                        "error": "engine could not open it"})
            continue
        try:
            h = _health(cb, tools, doc)
        except Exception as exc:  # noqa: BLE001
            out.append({"path": str(p), "opened": True, "watertight": False,
                        "free_edges": None, "non_manifold_edges": None,
                        "volume_mm3": 0.0, "bbox_size_mm": None, "bodies": 0,
                        "error": f"health raised {type(exc).__name__}: {exc}"})
            continue
        bodies = [b for b in (h.get("bodies") or []) if isinstance(b, dict)]
        vol = sum(float(b.get("volume_mm3") or 0.0) for b in bodies)
        free = sum(int(((b.get("free_edges") or {}).get("count")) or 0) for b in bodies)
        nonman = sum(int(((b.get("non_manifold_edges") or {}).get("count")) or 0)
                     for b in bodies)
        size = None
        try:
            bb = cb.call("doc_bbox", {"doc_id": doc})
            if isinstance(bb, dict) and bb.get("size"):
                size = [round(float(x), 4) for x in bb["size"]]
        except Exception:  # noqa: BLE001
            size = None
        out.append({
            "path": str(p), "opened": True,
            "watertight": bool(h.get("watertight")) and bool(bodies),
            "free_edges": free, "non_manifold_edges": nonman,
            "volume_mm3": round(vol, 4), "bbox_size_mm": size, "bodies": len(bodies),
            "verdict": str(h.get("summary") or "")[:240],
        })
    return out


def probe(run_dir: Path, record: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(part_facts, file_facts) for one prompt's run directory."""
    dirs = [run_dir / "work", run_dir / "autosave"]
    files = file_facts([run_dir / "work"])
    if record:
        known = {f["path"] for f in files}
        files += [f for f in reported_files(record) if f["path"] not in known]
    return part_facts(dirs), files


def engine_fingerprint() -> dict:
    """What engine the probe (and the turns) actually used, plus the staleness check the
    benchmark refuses to run without. A stale engine has invalidated live sweeps here
    before; recording it is how a stored result stays trustworthy."""
    lib = os.environ.get("CHUDWARE_LIBCHUDWARE") or ""
    repo = Path(__file__).resolve().parents[2]
    info: dict = {"engine": lib or "(resolver default)",
                  "daemon_mode": os.environ.get("CHUDWARE_ENGINE_DAEMON", "1") != "0"}
    p = Path(lib) if lib else None
    if p and p.is_file():
        info["engine_mtime"] = int(p.stat().st_mtime)
        newer = [str(s.relative_to(repo))
                 for s in (repo / "core").rglob("*")
                 if s.is_file() and s.suffix in (".cpp", ".h")
                 and s.stat().st_mtime > p.stat().st_mtime]
        # core/tests/* are test translation units; they are not linked into
        # libchudware.so, so they cannot make the library stale.
        info["stale_sources"] = sorted(s for s in newer if not s.startswith("core/tests/"))
        info["stale_test_sources"] = sorted(s for s in newer if s.startswith("core/tests/"))
    else:
        info["engine_mtime"] = None
        info["stale_sources"] = ["(unknown: CHUDWARE_LIBCHUDWARE not a file)"]
    return info
