"""Evidence: ONE declared final artifact, reopened in an in-process engine.

Every check of a trial reads the same :class:`Evidence`, built once from the exact path
the task declares (``<workspace>/<task.final_artifact>``). Nothing here scans the
workspace for "the best" document: a criterion satisfied by some other file does not
count.

The engine is the in-process ``libchudware`` (``CHUDWARE_ENGINE_DAEMON=0``, forced before
``core_bridge`` is imported): grading never attaches to the user's ``chudware-hostd``,
never deposits documents into its pool, and never shares state with the turn's engine.

:func:`build_evidence` never raises. A missing/unopenable file is recorded in
``exists``/``open_error`` (an artifact deficit -> checks FAIL); an engine that cannot run at
all is recorded in ``engine_error`` (an infrastructure fault -> checks ERROR). Use
:func:`gate` at the top of a check to apply that split uniformly.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"

from benchmarks.artifact_validity.tasks.schema import CheckResult, Status  # noqa: E402


def _cb():
    from services.mcp import core_bridge  # noqa: PLC0415 — engine import is the slow part

    return core_bridge


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Evidence:
    final_path: Path
    exists: bool
    sha256: str | None = None
    doc_id: str | None = None
    open_error: str | None = None
    bodies: list[dict] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)
    studies: list[dict] = field(default_factory=list)
    # -- extras beyond the minimal contract --------------------------------------------
    #: Engine could not run at all (import/ping failed): checks must report ERROR.
    engine_error: str | None = None
    size_bytes: int | None = None
    mtime: float | None = None
    #: Raw ``doc_tree.tree`` (material, units, equations, schema) for checks that need it.
    tree: dict = field(default_factory=dict)
    #: Native revision counters (``doc_tree.revision`` / ``sim_study_list.revision``).
    revision: Any = None
    study_revision: Any = None
    #: Inventory sub-steps that failed on an OPENED document, keyed by RPC name.
    probe_errors: dict[str, str] = field(default_factory=dict)

    @property
    def opened(self) -> bool:
        return self.doc_id is not None

    def rpc(self, method: str, params: dict | None = None) -> dict:
        """Call the grader engine with this document's ``doc_id`` injected when absent.

        Raises (``CoreError``/``RuntimeError``) like ``core_bridge.call``; a check that wants
        FAIL-on-error must catch. Calling with no opened document raises RuntimeError."""
        if self.doc_id is None:
            raise RuntimeError(
                f"no opened document for {self.final_path} "
                f"({self.engine_error or self.open_error or 'missing'})")
        p = dict(params or {})
        p.setdefault("doc_id", self.doc_id)
        out = _cb().call(method, p)
        return out if isinstance(out, dict) else {"value": out}

    def study(self, type: str, name: str | None = None) -> dict | None:  # noqa: A002
        """The LAST saved study of ``type`` (optionally with exact ``name``), or None."""
        hits = [s for s in self.studies
                if isinstance(s, dict) and s.get("type") == type
                and (name is None or s.get("name") == name)]
        return hits[-1] if hits else None

    def summary(self) -> dict:
        """JSON-safe inventory recorded in the trial manifest."""
        return {
            "final_path": str(self.final_path), "exists": self.exists,
            "sha256": self.sha256, "size_bytes": self.size_bytes, "mtime": self.mtime,
            "doc_id": self.doc_id, "open_error": self.open_error,
            "engine_error": self.engine_error, "revision": self.revision,
            "study_revision": self.study_revision, "probe_errors": self.probe_errors,
            "bodies": self.bodies,
            "features": [{k: f.get(k) for k in ("id", "type", "name", "suppressed",
                                                 "failed", "error")}
                         for f in self.features],
            "studies": [{"id": s.get("id"), "type": s.get("type"), "name": s.get("name"),
                         "result_keys": sorted((s.get("results") or {}).keys())
                         if isinstance(s.get("results"), dict) else None}
                        for s in self.studies if isinstance(s, dict)],
        }


def gate(ev: Evidence, check_id: str) -> CheckResult | None:
    """Uniform artifact precondition for a check.

    Returns ERROR when the grader engine is down, FAIL when the declared artifact is
    missing, empty or unopenable, and None when the check may proceed."""
    if ev.engine_error:
        return CheckResult(check_id, Status.ERROR, detail=f"grader engine: {ev.engine_error}")
    if not ev.exists:
        return CheckResult(check_id, Status.FAIL, measured="missing",
                           expected=str(ev.final_path.name),
                           detail=f"declared artifact not found: {ev.final_path}")
    if ev.doc_id is None:
        return CheckResult(check_id, Status.FAIL, measured="unopenable",
                           detail=f"declared artifact did not open: {ev.open_error}")
    return None


def _no_bodies(exc: Exception) -> bool:
    """The engine's reply for a document that simply has no solid bodies."""
    return "no bodies" in str(exc).lower()


def _bodies(ev: Evidence) -> list[dict]:
    """World bodies (name/bbox) joined with per-body solid health (volume/validity)."""
    world: list[dict] = []
    try:
        world = [b for b in (ev.rpc("asm_world_bodies", {"explode": False}).get("bodies")
                             or []) if isinstance(b, dict)]
    except Exception as exc:  # noqa: BLE001
        if not _no_bodies(exc):
            ev.probe_errors["asm_world_bodies"] = f"{type(exc).__name__}: {exc}"
    else:
        if not world:
            # A body-less document (e.g. a study-only deliverable) is a fact, not a fault.
            return []
    health: dict[Any, dict] = {}
    try:
        for h in ev.rpc("eval_solid_health", {"body": 0}).get("bodies") or []:
            if isinstance(h, dict):
                health[h.get("body_id")] = h
    except Exception as exc:  # noqa: BLE001
        if not (_no_bodies(exc) and not world):
            ev.probe_errors["eval_solid_health"] = f"{type(exc).__name__}: {exc}"
    out: list[dict] = []
    ids = [b.get("body_id") for b in world] or list(health)
    by_world = {b.get("body_id"): b for b in world}
    for bid in ids:
        w = by_world.get(bid, {})
        h = health.get(bid, {})
        out.append({
            "id": bid,
            "name": w.get("name"),
            "component": w.get("component"),
            "volume_mm3": h.get("volume_mm3"),
            "bbox": w.get("bbox"),
            "valid": h.get("brep_valid"),
            "closed": h.get("is_watertight"),
            "sound": h.get("sound"),
            "solids": h.get("solids"),
            "faces": h.get("faces"),
        })
    return out


def build_evidence(final_path: Path) -> Evidence:
    """Open ``final_path`` in the in-process engine and inventory it. Never raises."""
    final_path = Path(final_path)
    ev = Evidence(final_path=final_path, exists=final_path.is_file())
    if not ev.exists:
        return ev
    try:
        st = final_path.stat()
        ev.size_bytes, ev.mtime = st.st_size, st.st_mtime
        ev.sha256 = sha256_file(final_path)
    except OSError as exc:
        ev.open_error = f"unreadable: {exc}"
        return ev
    if not ev.size_bytes:
        ev.open_error = "file is empty"
        return ev
    try:
        cb = _cb()
        cb.call("ping", {})
    except Exception as exc:  # noqa: BLE001
        ev.engine_error = f"{type(exc).__name__}: {exc}"
        return ev
    try:
        opened = cb.call("doc_open", {"path": str(final_path.resolve())})
    except Exception as exc:  # noqa: BLE001 — an unopenable deliverable is a FAIL, not a crash
        ev.open_error = f"{type(exc).__name__}: {exc}"
        return ev
    if not isinstance(opened, dict) or not opened.get("doc_id"):
        ev.open_error = f"doc_open returned no doc_id: {str(opened)[:300]}"
        return ev
    ev.doc_id = str(opened["doc_id"])
    status = {str(r.get("id")): r for r in
              ((opened.get("rebuild") or {}).get("feature_status") or [])
              if isinstance(r, dict)}
    try:
        state = ev.rpc("doc_tree")
        ev.tree = state.get("tree") or {}
        ev.revision = state.get("revision")
        for f in ev.tree.get("features") or []:
            if not isinstance(f, dict):
                continue
            row = status.get(str(f.get("id"))) or {}
            ev.features.append({
                "id": f.get("id"), "type": f.get("type"), "name": f.get("name"),
                "params": f.get("params") or {}, "refs": f.get("refs") or {},
                "suppressed": bool(f.get("suppressed")),
                "failed": bool(row) and not row.get("ok", True),
                "error": row.get("error"),
                "rolled_back": bool(row.get("rolled_back")),
            })
    except Exception as exc:  # noqa: BLE001
        ev.probe_errors["doc_tree"] = f"{type(exc).__name__}: {exc}"
    ev.bodies = _bodies(ev)
    try:
        listing = ev.rpc("sim_study_list")
        ev.studies = [s for s in listing.get("studies") or [] if isinstance(s, dict)]
        ev.study_revision = listing.get("revision")
    except Exception as exc:  # noqa: BLE001
        ev.probe_errors["sim_study_list"] = f"{type(exc).__name__}: {exc}"
    return ev
