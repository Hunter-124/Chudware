"""C++ feature-engine + simulation MCP tools — parity with the in-app AI-CAD agent.

These tools drive the SAME engine the desktop app uses (``libaicad.dll`` via
``core_bridge``): a real parametric feature kernel with an editable feature tree, the
plain-English geometry vocabulary (address faces/edges in words — "the top face", "the
vertical edges" — no coordinates or face numbers), FEA (static + modal), the LBM
wind-tunnel CFD, the labelled/colour-coded vision render, 2D drawings, and the
standard-parts library.

The intended driver is Claude itself (in Claude cowork/chat or Claude Code) — it is the
"brain" the in-app agent delegates to a local LLM. So the first tool, ``cad_guide``,
hands Claude the engine's own modeling cheatsheet (the closed op vocabulary + worked
examples). Typical loop:

    cad_guide()                  # learn the op vocabulary (call this FIRST)
    new_part("bracket")
    add_features([...ops...])    # build with standard editable features
    render_part()                # SEE it — labelled, colour-coded; self-review
    ... iterate ...
    run_fea_static(...)          # prove it holds
    export_part("step")          # or make_drawing()

Registered onto the FastMCP instance by ``register_cad_tools(mcp)``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

from mcp.server.fastmcp import FastMCP, Image

from . import core_bridge as cb
from .core_bridge import CoreError

# The "current part" so single-argument calls just work (every tool also takes an explicit
# doc_id). Mirrors the old server's _STATE, but the doc lives in the C++ core, not here.
_CUR: dict = {"doc_id": None}


# --------------------------------------------------------------------------- helpers

def _doc(doc_id: Optional[str]) -> str:
    d = doc_id or _CUR["doc_id"]
    if not d:
        raise ValueError("No current part. Call new_part(...) first (or pass doc_id=).")
    return d


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2)


def _err(e: Exception) -> str:
    """Render an engine error as a structured, fixable result (not a transport failure)."""
    if isinstance(e, CoreError):
        out = {"ok": False, "error": str(e)}
        if e.code:
            out["code"] = e.code
        if e.detail is not None:
            out["detail"] = e.detail
        return _dumps(out)
    return _dumps({"ok": False, "error": str(e)})


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="aicad_mcp_")
    os.close(fd)
    return path


# --- live viewport handoff -------------------------------------------------------------
# The MCP runs in its OWN process with its own engine, so the running desktop app can't see
# its doc sessions. To make the AI's part visible in the app's viewport, we publish it as a
# native .step+ doc plus a tiny live.json {rev, step} into %APPDATA%\AI-CAD\handoff\; the host
# polls that control file each frame (ui::pollAiCadHandoff) and opens/refreshes one tab from it.
_PUB: dict = {"rev": None}


def _handoff_dir() -> Path:
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    d = Path(base) / "AI-CAD" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _publish_current(doc_id: str) -> Optional[dict]:
    """Publish `doc_id` to the handoff dir so the open app shows it. Best-effort; never raises."""
    try:
        d = _handoff_dir()
        step = d / "live.step+"
        cb.call("doc_save", {"doc_id": doc_id, "path": str(step)})
        if _PUB["rev"] is None:
            # Seed from any existing control file so the rev keeps rising across MCP restarts.
            try:
                _PUB["rev"] = int(json.loads((d / "live.json").read_text()).get("rev", 0))
            except Exception:
                _PUB["rev"] = 0
        _PUB["rev"] += 1
        ctrl = {"rev": _PUB["rev"], "step": str(step), "doc_id": doc_id}
        tmp = d / "live.json.tmp"
        tmp.write_text(json.dumps(ctrl))
        os.replace(tmp, d / "live.json")  # atomic: the host never reads a half-written control file
        return {"shown_in_app": True, "rev": _PUB["rev"]}
    except Exception:
        return None


def _autosave_dir() -> Path:
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    d = Path(base) / "AI-CAD" / "mcp_autosave"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _autosave(doc_id: Optional[str]) -> None:
    """Persist a doc to a recoverable .step+ so a server restart (e.g. after a client timeout)
    can reload it instead of dropping every open document. Best-effort; never raises."""
    if not doc_id:
        return
    try:
        cb.call("doc_save", {"doc_id": doc_id, "path": str(_autosave_dir() / f"{doc_id}.step+")})
    except Exception:  # noqa: BLE001
        pass


def recover_autosaved() -> list:
    """Reopen every autosaved .step+ into the engine (called at server startup). A reopened doc
    gets a FRESH doc_id; returns [{path, doc_id}] for the ones that loaded. Best-effort."""
    out: list = []
    try:
        files = sorted(_autosave_dir().glob("*.step+"))
    except Exception:  # noqa: BLE001
        return out
    for p in files:
        try:
            r = cb.call("doc_open", {"path": str(p)})
            did = r.get("doc_id")
            if did:
                out.append({"path": str(p), "doc_id": did})
                _CUR["doc_id"] = did
        except Exception:  # noqa: BLE001
            continue
    return out


# --- async jobs ------------------------------------------------------------------------
# Heavy solves (CFD, FEA) can exceed the MCP transport's per-call timeout; when the client gives
# up it may KILL the server and lose every open doc. So the sim tools can run in the BACKGROUND:
# return a job_id immediately, run the engine call on a worker thread (the LBM/FEA solve itself
# does not hold the engine lock), and let the caller poll get_job(job_id). Default stays synchronous
# for simple use; pass background=True for the heavy runs.
import threading as _threading  # noqa: E402
import uuid as _uuid  # noqa: E402

_JOBS: dict = {}
_JOB_LOCK = _threading.Lock()


def _start_job(kind: str, fn, progress: Optional[dict] = None) -> str:
    jid = "job-" + _uuid.uuid4().hex[:12]
    with _JOB_LOCK:
        _JOBS[jid] = {"status": "running", "kind": kind, "result": None, "error": None,
                      "progress": progress if progress is not None else {}}

    def _run() -> None:
        try:
            res = fn()
            with _JOB_LOCK:
                _JOBS[jid].update(status="done", result=res)
        except Exception as e:  # noqa: BLE001
            with _JOB_LOCK:
                _JOBS[jid].update(status="error", error=str(e))

    # Daemon thread: a long OpenFOAM/FEA solve must never block server (or test) shutdown.
    _threading.Thread(target=_run, name=f"aicad-job-{kind}", daemon=True).start()
    return jid


# A small material library so FEA tools take a friendly name. Fields are exactly what the
# host's study expects (host/src/ui/sim_manager.cpp): E in MPa, density g/cm^3, strengths MPa.
_MATERIALS: dict = {
    "steel": {"name": "Mild steel (A36)", "E_mpa": 200000, "nu": 0.29, "density_g_cm3": 7.85,
              "yield_mpa": 250, "ultimate_mpa": 400, "endurance_mpa": 200},
    "stainless": {"name": "Stainless 304", "E_mpa": 193000, "nu": 0.29, "density_g_cm3": 8.0,
                  "yield_mpa": 215, "ultimate_mpa": 505, "endurance_mpa": 240},
    "aluminum": {"name": "Aluminium 6061-T6", "E_mpa": 69000, "nu": 0.33, "density_g_cm3": 2.70,
                 "yield_mpa": 276, "ultimate_mpa": 310, "endurance_mpa": 96},
    "titanium": {"name": "Titanium Ti-6Al-4V", "E_mpa": 113800, "nu": 0.342, "density_g_cm3": 4.43,
                 "yield_mpa": 880, "ultimate_mpa": 950, "endurance_mpa": 510},
    "brass": {"name": "Brass C360", "E_mpa": 97000, "nu": 0.34, "density_g_cm3": 8.5,
              "yield_mpa": 124, "ultimate_mpa": 338, "endurance_mpa": 120},
    "abs": {"name": "ABS plastic", "E_mpa": 2300, "nu": 0.35, "density_g_cm3": 1.04,
            "yield_mpa": 40, "ultimate_mpa": 44, "endurance_mpa": 15},
    "pla": {"name": "PLA plastic", "E_mpa": 3500, "nu": 0.36, "density_g_cm3": 1.24,
            "yield_mpa": 50, "ultimate_mpa": 50, "endurance_mpa": 18},
}
_MATERIAL_ALIASES: dict = {
    "mild steel": "steel", "carbon steel": "steel", "a36": "steel",
    "ss": "stainless", "304": "stainless", "stainless steel": "stainless",
    "aluminium": "aluminum", "al": "aluminum", "6061": "aluminum", "alu": "aluminum",
    "ti": "titanium", "ti-6al-4v": "titanium",
}


def _resolve_material(material: Union[str, dict, None]) -> dict:
    if isinstance(material, dict):
        return material
    key = (material or "steel").strip().lower()
    key = _MATERIAL_ALIASES.get(key, key)
    if key not in _MATERIALS:
        raise ValueError(
            f"Unknown material {material!r}. Known: {sorted(_MATERIALS)} "
            f"(or pass an explicit dict with E_mpa/nu/density_g_cm3/yield_mpa)."
        )
    return _MATERIALS[key]


def _as_face_query(q: Union[str, dict]) -> dict:
    """Accept a full geometry query dict OR a bare side word ("top", "+z") -> a face query."""
    if isinstance(q, dict):
        return q
    return {"of": "face", "side": str(q)}


def _resolve_face_points(doc_id: str, query: Union[str, dict]) -> list:
    """Plain-English face query -> a list of {points:[...]} face-point sets (one per face)."""
    res = cb.call("doc_query", {"doc_id": doc_id, "query": _as_face_query(query)})
    matches = [m for m in res.get("matches", []) if m.get("kind") == "face"]
    if not matches:
        raise ValueError(f"No face matched query {query!r} (try query_geometry to explore).")
    out = []
    for sel in matches:
        fp = cb.call("doc_face_points", {"doc_id": doc_id, "selector": sel})
        if fp.get("points"):
            out.append(fp)
    if not out:
        raise ValueError(f"Query {query!r} matched faces but no surface points resolved.")
    return out


# --------------------------------------------------------------------- the guide text

_GUIDE_PROMPTS = ["overview", "review", "simulation"]


def _prompts_dir() -> Path:
    d = os.environ.get("AI_CAD_PROMPTS_DIR")
    if d and Path(d).is_dir():
        return Path(d)
    # Fall backs: the staged dist prompts, then the repo source prompts.
    staged = cb.dist_dir() / "app" / "agent" / "prompts"
    if staged.is_dir():
        return staged
    return cb._repo_root() / "agent" / "prompts"


def _read_prompt(rel: str) -> str:
    p = _prompts_dir() / rel
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


_SIM_GUIDE = """\
SIMULATION GUIDE — FEA + CFD on the live part.

Faces for fixtures and loads are named with the SAME plain-English geometry queries you
build with: a bare side word ("bottom", "+z") or a full query ({"of":"face","side":"bottom",
"pick":"largest"}). Use query_geometry(...) first if unsure what faces a part has.

run_fea_static(fixtures, loads, material) — linear static stress.
  fixtures : list of face queries that are held fixed (encastre). e.g. ["bottom"].
  loads    : list of dicts, each a face query plus a force or pressure:
               {"face": "top", "force": [0, 0, -500]}      # newtons, world XYZ
               {"face": "top", "pressure": 0.5}            # MPa, inward on the face
  material : "steel" | "aluminum" | "titanium" | "stainless" | "brass" | "abs" | "pla"
             (or an explicit dict with E_mpa / nu / density_g_cm3 / yield_mpa).
  Returns max von-Mises stress (MPa), max displacement (mm), and the min factor of safety
  (yield / max stress). FoS > 1 means it holds; design to a target margin.

run_fea_modal(fixtures, material, n_modes) — natural frequencies (Hz) of the held part.

run_cfd_flow(velocity, aoa, fluid) — LBM wind tunnel over the whole part/assembly.
  velocity m/s, aoa degrees, fluid "air"|"water". Returns drag (N), lift (N), Cd, frontal
  area, and Reynolds number. Coarse grid by default — a directional estimate, raise `cells`
  for more resolution.
  CONVENTIONS (Z-up): freestream +X, lift +Z, span Y; +aoa pitches nose up. `reynolds`/`cd`
  use the part's LONGEST span as reference length (reported as `ref_length_mm`).
  TRUST THE DIAGNOSTICS: the result reports `voxel_mm` / `min_feature_voxels` (thin features
  under ~8 voxels are unresolved → lift reads ~0, a `warning` is set — raise `cells` or run the
  thin part alone) and `converged` / `residual` (a non-converged run is under-iterated, not
  physically zero). For heavy runs pass background=True and poll get_job(job_id).

run_cfd_accurate(velocity, aoa, fluid, preset) — HIGH-FIDELITY OpenFOAM RANS (k-omega SST + prism
  layers). The TRUSTWORTHY absolute Cd/Cl path (validated ~10-15% of experiment) vs run_cfd_flow's
  coarse LBM estimate. Requires OpenFOAM (WSL/native); runs for MINUTES, always background → poll
  get_job(job_id) (live progress). preset coarse|medium|fine|xfine. Use run_cfd_flow first to scan
  trends fast, then run_cfd_accurate to certify the chosen design.

Heavy solves: run_cfd_flow / run_fea_* accept background=True (run_cfd_accurate is always
background) → return a job_id immediately; poll get_job(job_id) for the result. This keeps a long
solve from tripping the transport timeout (which would otherwise drop the session). After a
restart, recover_parts() reloads auto-saved docs.
"""

_OVERVIEW_HEADER = """\
AI-CAD MODELING GUIDE (C++ parametric engine — same as the desktop app).

WORKFLOW
  1. new_part(name)                         start an editable part document.
  2. add_features([ ...ops... ])            build with standard features (vocabulary below).
                                            Returns per-feature status + body count + volume
                                            so you can see a dropped cut or a stray body.
  3. render_part()                          SEE it — an annotated, colour-coded image (faces
                                            tinted + labelled TOP/FRONT/RIGHT...). Self-review
                                            against the request; iterate add_features to fix.
  4. measure_part() / query_geometry(q)     check mass/bbox, or explore faces/edges.
  5. run_fea_static(...) / run_cfd_flow(...) prove it (see cad_guide("simulation")).
  6. save_part()                            save the NATIVE .step+ — the editable feature tree
                                            PLUS its simulation studies + any drawings, in one
                                            file the desktop app reopens fully editable. This is
                                            AI-CAD's native format; save to it to hand the user a
                                            part they can keep working on (not a flattened mesh).
  7. export_part(fmt) / make_drawing()      hand off a NON-native interchange/mesh file
                                            (STEP/IGES/BREP exact, STL/OBJ/3MF mesh) or a
                                            dimensioned 2D drawing.

Address geometry in PLAIN WORDS (no coordinates, no face numbers): "the top face", "all
edges", "the vertical edges", "the largest round face". The engine resolves the words to
topology. cad_guide("review") explains how to read the annotated render. The full op
vocabulary, with worked examples, follows.

================================================================================
"""


def cad_guide_text(topic: str = "overview") -> str:
    topic = (topic or "overview").strip().lower()
    if topic in ("overview", "features", "ops", "geometry", "vocabulary", "vocab"):
        return _OVERVIEW_HEADER + _read_prompt("shared/ops_cheatsheet.md")
    if topic in ("review", "render", "vision"):
        return _read_prompt("reviewer.md") or "(reviewer guide unavailable)"
    if topic in ("simulation", "sim", "fea", "cfd"):
        return _SIM_GUIDE
    # Otherwise treat the topic as a per-feature cheatsheet name (hole, fillet, revolve, ...).
    sheet = _read_prompt(f"shared/cheatsheets/{topic}.md")
    if sheet:
        return sheet
    avail = sorted(p.stem for p in (_prompts_dir() / "shared" / "cheatsheets").glob("*.md")) \
        if (_prompts_dir() / "shared" / "cheatsheets").is_dir() else []
    return (f"No guide for {topic!r}. Topics: overview, simulation, review; or a feature "
            f"cheatsheet: {avail}")


# --------------------------------------------------------------------------- register

def register_cad_tools(mcp: FastMCP) -> None:
    """Attach the C++ feature-engine + simulation tools to the FastMCP server."""

    # ---------------------------------------------------------------- knowledge

    @mcp.tool()
    def cad_guide(topic: str = "overview") -> str:
        """Return the engine's modeling/simulation knowledge so you can drive it well. CALL FIRST.

        This is the same closed op vocabulary + worked examples the in-app agent's modeler is
        taught. topic="overview" (default) gives the workflow + full op cheatsheet (primitives,
        sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell/draft, holes, bolt_circle,
        patterns/mirror, edits) and the plain-English geometry-query predicates. Other topics:
        "simulation" (FEA/CFD how-to), "review" (how to read the annotated render), or a single
        feature's cheatsheet by name ("hole", "fillet", "revolve", "loft", "sweep", "shell",
        "chamfer", "pattern", "mirror", "sketch", "airfoil", "drawing", "hardware", ...).
        """
        return cad_guide_text(topic)

    # ---------------------------------------------------------------- modeling

    @mcp.tool()
    def new_part(name: str = "part") -> str:
        """Create a new, empty editable part document and make it current. Returns its doc_id.

        Follow with add_features(...) to build geometry. Each part is a real parametric feature
        tree (undo/redo, editable parameters), not a one-shot mesh.
        """
        try:
            r = cb.call("doc_new", {"kind": "part", "name": name})
            _CUR["doc_id"] = r.get("doc_id")
            return _dumps({"ok": True, "doc_id": r.get("doc_id"), "name": name,
                           "revision": r.get("revision", 0)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_features(ops: list, doc_id: Optional[str] = None) -> str:
        """Apply a list of document ops to build/edit the part (the core modeling tool).

        `ops` is a JSON array of ops in the vocabulary from cad_guide() — e.g.
          [{"op":"add_feature","feature":{"type":"box","name":"Block",
              "params":{"length":{"value":40,"expr":null},"width":{"value":30,"expr":null},
                        "height":{"value":20,"expr":null}}}},
           {"op":"add_feature","feature":{"type":"fillet","name":"Round",
              "params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}]
        Dimensions are objects {"value":<n>,"expr":null}. Geometry is addressed in plain words
        via `refs` (e.g. {"face":{"of":"face","side":"top"}}). Ops in one call may reference an
        earlier op in the SAME array as "$<index>" (0-based); existing tree features by id ("f1").

        Returns revision, body_count, volume_mm3, and per-feature status (ok / error / warning)
        so you can immediately tell a dropped cut, a failed reference, or a stray extra body.
        Then call render_part() to LOOK at the result. On a validation error the whole batch is
        rejected — read `error`, fix the ops, call again.
        """
        try:
            d = _doc(doc_id)
            # Transactional batch: snapshot first so a partial failure rolls the WHOLE batch back
            # (the engine otherwise leaves failed features in the tree). All-or-nothing.
            ckpt = None
            try:
                ckpt = cb.call("doc_checkpoint", {"doc_id": d}).get("checkpoint")
            except Exception:  # noqa: BLE001
                ckpt = None

            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
            fs = (r.get("rebuild") or {}).get("feature_status", [])
            feats = [{"id": s.get("id"), "ok": s.get("ok"),
                      "error": s.get("error"), "warning": s.get("warning")} for s in fs]
            bodies = (r.get("tess") or {}).get("bodies", [])
            ok = all(s.get("ok") for s in fs) if fs else True

            if not ok and ckpt:
                # Roll the tree back to the pre-batch revision; report WHY each feature failed.
                reverted = False
                try:
                    cb.call("doc_revert", {"doc_id": d, "checkpoint": ckpt})
                    reverted = True
                except Exception:  # noqa: BLE001
                    reverted = False
                fail = [{"id": s.get("id"), "error": s.get("error")} for s in fs
                        if not s.get("ok")]
                _publish_current(d)  # mirror the rolled-back (or current) state to the app
                return _dumps({
                    "ok": False,
                    "doc_id": d,
                    "rolled_back": reverted,
                    "error": "batch rejected — no features were kept; fix and resend"
                             if reverted else "batch failed and rollback failed",
                    "failed_features": fail,
                    "features": feats,
                })

            out = {
                "ok": ok,
                "doc_id": d,
                "revision": r.get("revision"),
                "body_count": len(bodies),
                "volume_mm3": r.get("volume"),
                "features": feats,
            }
            # Push the updated part to the desktop app's viewport (if it's open) so the user
            # watches it build. Best-effort; only when something actually got built.
            if ok and bodies:
                pub = _publish_current(d)
                if pub:
                    out["shown_in_app"] = True
                _autosave(d)  # persist so a server restart can recover this doc
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def get_part(doc_id: Optional[str] = None) -> str:
        """Return the current feature tree (features, parameters, ids) + revision for editing."""
        try:
            return _dumps(cb.call("doc_tree", {"doc_id": _doc(doc_id)}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def query_geometry(query: Union[str, dict], doc_id: Optional[str] = None) -> str:
        """Resolve a plain-English face/edge query against the part — explore/confirm geometry.

        `query` is a geometry query like {"of":"face","side":"top"}, {"of":"edge","parallel":"z"},
        {"of":"face","surf":"round","pick":"outer"} — or a bare side word ("top") for a face.
        Returns each matching face/edge with its shape (surf), side, centroid and size, so you can
        confirm "the top face" resolves before you reference it in a feature or a simulation.
        """
        try:
            q = query if isinstance(query, dict) else _as_face_query(query)
            return _dumps(cb.call("doc_query", {"doc_id": _doc(doc_id), "query": q}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def render_part(doc_id: Optional[str] = None) -> Image:
        """Render the current part to an ANNOTATED image and return it so you can SEE the result.

        The render is colour-coded and labelled exactly like the in-app vision reviewer: an XYZ
        orientation gizmo, flat faces tinted by direction with the principal ones labelled
        (TOP +Z blue, BOTTOM −Z teal, FRONT −Y yellow, BACK +Y green, RIGHT +X red, LEFT −X
        orange), curved faces left grey. Use it to verify features landed on the face the request
        named and the proportions look right. See cad_guide("review") for the full key.
        """
        d = _doc(doc_id)
        png = _tmp(".png")
        r = cb.call("doc_render_png", {"doc_id": d, "out_path": png})
        if not r.get("ok") or not os.path.exists(png):
            raise ValueError(f"Render failed: {r.get('reason', 'no bodies to render')}")
        return Image(path=png)

    @mcp.tool()
    def measure_part(doc_id: Optional[str] = None, density_g_cm3: float = 7.85) -> str:
        """Mass properties of the current part: volume (mm^3), mass (g), centre of mass, inertia.

        density_g_cm3 sets the material density used for mass (default 7.85 = steel). Also runs a
        geometry sanity check (closed/valid solid).

        Multi-body docs: the top-level volume/mass/com/inertia are the COMBINED total of ALL
        bodies, and `mass.bodies[]` lists each body's own properties (with `mass.body_count`).
        Pass body_id via a direct query if you need just one. (Previously this silently reported
        only the last body of an assembly.)
        """
        try:
            d = _doc(doc_id)
            mass = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": density_g_cm3})
            out = {"ok": True, "doc_id": d, "mass": mass}
            try:
                out["check"] = cb.call("eval_check", {"doc_id": d})
            except CoreError:
                pass
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def undo(doc_id: Optional[str] = None) -> str:
        """Undo the last edit on the current part."""
        try:
            d = _doc(doc_id)
            r = cb.call("doc_undo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def redo(doc_id: Optional[str] = None) -> str:
        """Redo the last undone edit on the current part."""
        try:
            d = _doc(doc_id)
            r = cb.call("doc_redo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def show_in_app(doc_id: Optional[str] = None) -> str:
        """Push the current part to the OPEN AI-CAD desktop app's 3D viewport so the user can see it.

        Modeling tools already do this automatically after each change; call this to force a refresh
        (e.g. after switching the current part). No-op effect if the desktop app isn't running — the
        part is written to the handoff folder and shown whenever the app next opens.
        """
        try:
            d = _doc(doc_id)
            pub = _publish_current(d)
            if not pub:
                return _dumps({"ok": False, "error": "could not publish (no geometry yet?)"})
            return _dumps({"ok": True, "doc_id": d, **pub})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_parts() -> str:
        """List all open part/drawing documents in this engine session."""
        try:
            return _dumps(cb.call("doc_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def close_part(doc_id: Optional[str] = None) -> str:
        """Close a part document and free it. Clears the current part if it was current."""
        try:
            d = _doc(doc_id)
            r = cb.call("doc_close", {"doc_id": d})
            if _CUR["doc_id"] == d:
                _CUR["doc_id"] = None
            return _dumps({"ok": True, "closed": r.get("closed", True), "doc_id": d})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def save_part(path: Optional[str] = None, doc_id: Optional[str] = None) -> str:
        """Save the part as AI-CAD's NATIVE .step+ — the editable, reopenable format. PREFER THIS.

        .step+ is the only format that round-trips the full PARAMETRIC feature tree (so the user
        can keep editing parameters/features), its SIMULATION STUDIES (FEA/CFD setups + results),
        and any associated 2D drawings — all in one file the desktop app opens fully editable. Use
        this to deliver a part the user will keep working on. (export_part writes flat interchange
        or mesh files: a plain STEP/STL has geometry only, no feature history or studies.)

        path : where to write. If omitted, a temp .step+ is written. A ".step+" extension is added
               if missing. Returns the written path and bytes so you can confirm a real save.
        """
        try:
            d = _doc(doc_id)
            out = path or _tmp(".step+")
            if not out.lower().endswith(".step+"):
                out += ".step+"
            r = cb.call("doc_save", {"doc_id": d, "path": out})
            written = r.get("path", out)
            ok = os.path.exists(written)
            return _dumps({"ok": ok, "format": "step+", "native": True, "doc_id": d,
                           "path": written,
                           "bytes": (os.path.getsize(written) if ok else 0)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def export_part(fmt: str = "step", path: Optional[str] = None,
                    doc_id: Optional[str] = None) -> str:
        """Export the current part to an interchange/mesh file; returns the written path.

        fmt: "step+" (native) | "step" | "stl" | "3mf" | "iges" | "brep" | "obj" | "ply" |
             "gltf" | "glb" | "3dm". If `path` is omitted, a temp file with the right extension is
             written. STEP/IGES/BREP are exact B-rep; STL/OBJ/PLY/glTF are tessellated meshes.

        NOTE: plain STEP/STL/etc. carry geometry only — NO editable feature tree or simulation
        studies. To hand the user a part they can keep editing, use save_part() (native .step+).
        fmt="step+" here is accepted as an alias and routed to the native saver.
        """
        try:
            d = _doc(doc_id)
            fmt = fmt.lower().lstrip(".")
            # The native editable container is written by doc_save, not the interchange writer.
            if fmt in ("step+", "stepplus", "stp+", "native"):
                out = path or _tmp(".step+")
                if not out.lower().endswith(".step+"):
                    out += ".step+"
                r = cb.call("doc_save", {"doc_id": d, "path": out})
                written = r.get("path", out)
                return _dumps({"ok": os.path.exists(written), "format": "step+",
                               "native": True, "doc_id": d, "path": written,
                               "exists": os.path.exists(written)})
            out = path or _tmp("." + fmt)
            # The engine round-trips B-rep formats (re-reads + BRepCheck) and THROWS if the file is
            # truncated/malformed, so a returned ok:True means it actually re-opens. It echoes real
            # metadata (solid_count, valid_solids, bytes, validated) — surface it so the caller can
            # trust the export instead of a bare exists flag.
            r = cb.call("export_file", {"doc_id": d, "path": out, "format": fmt})
            written = r.get("path", out)
            res = {"ok": True, "format": fmt, "doc_id": d, "path": written,
                   "exists": os.path.exists(written)}
            for k in ("bytes", "solid_count", "valid_solids", "expected_solids", "validated"):
                if k in r:
                    res[k] = r[k]
            return _dumps(res)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def import_part(path: str, into: Optional[str] = None, as_solid: bool = False) -> str:
        """Import a CAD/mesh file (STEP/IGES/BREP/STL/3MF/OBJ/STEP+/…) as a body.

        Lets you assemble components built or exported separately, or recover geometry from a file
        after a session reset. `into` = an existing doc_id to add the body to the CURRENT part
        (for assembly); omit it to open the file as a NEW part (made current). `as_solid` sews a
        mesh (STL/OBJ/…) into a solid on import; B-rep formats already import as solids.

        TIP: pair with the `transform` feature (in add_features) to POSITION each imported body
        into a shared coordinate frame — translate:[x,y,z] and/or angle:<deg> with rotate:[ax,ay,az].
        """
        try:
            params: dict = {"path": path, "as_solid": bool(as_solid)}
            if into:
                params["into"] = into
            r = cb.call("import_file", params)
            new_id = r.get("doc_id") or into
            if new_id and not into:
                _CUR["doc_id"] = new_id
            if new_id:
                _publish_current(new_id)
            return _dumps({"ok": True, "doc_id": new_id, "imported_from": path,
                           "into": into, **{k: r[k] for k in ("revision", "name", "body_count")
                                            if k in r}})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- simulation

    @mcp.tool()
    def run_fea_static(fixtures: list, loads: list, material: Union[str, dict] = "steel",
                       mesh_fineness: float = 0.5, doc_id: Optional[str] = None,
                       background: bool = False) -> str:
        """Linear-static stress FEA on the current part. Returns max stress, displacement, and FoS.

        fixtures : list of face queries held fixed, e.g. ["bottom"] or
                   [{"of":"face","side":"bottom","pick":"largest"}].
        loads    : list of {face query + a force or pressure}, e.g.
                   [{"face":"top","force":[0,0,-500]}]   # newtons, world XYZ
                   [{"face":"top","pressure":0.5}]       # MPa, inward
        material : a name ("steel","aluminum","titanium","stainless","brass","abs","pla") or an
                   explicit dict {E_mpa, nu, density_g_cm3, yield_mpa, ...}.
        mesh_fineness : 0..1 (default 0.5). Second-order (tet10) elements are used for accuracy.
        background : if True, return a job_id immediately and solve on a worker thread — poll
                     get_job(job_id). Use it for fine meshes that may exceed the call timeout.

        Returns {max_von_mises_mpa, max_disp_mm, min_fos}. min_fos = yield / max stress; > 1 holds.
        Tip: query_geometry(...) first to confirm your fixture/load faces resolve.
        """
        try:
            d = _doc(doc_id)
            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})

            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            if not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved"})

            load_specs: list = []
            for ld in loads:
                if not isinstance(ld, dict) or "face" not in ld:
                    raise ValueError(f"each load needs a 'face' query: {ld!r}")
                faces = _resolve_face_points(d, ld["face"])
                merged = {"points": []}
                for f in faces:
                    merged["points"].extend(f.get("points", []))
                spec: dict = {"faces": merged}
                if "pressure" in ld:
                    spec["type"] = "pressure"
                    spec["magnitude"] = float(ld["pressure"])
                elif "force" in ld:
                    spec["type"] = "force"
                    spec["vector"] = [float(x) for x in ld["force"]]
                else:
                    raise ValueError(f"load needs 'force':[x,y,z] or 'pressure':<MPa>: {ld!r}")
                load_specs.append(spec)

            study = {
                "step_path": step,
                "material": mat,
                "fixtures": fixture_sets,
                "loads": load_specs,
                "mesh_order": 2,        # tet10 captures bending
                "fineness": mesh_fineness,
            }

            def _solve() -> dict:
                res = cb.call("sim_run_static", {"study": study})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d, "error": res.get("error", "solve failed")}
                return {"ok": True, "doc_id": d, "material": mat.get("name", material),
                        "summary": res.get("summary", {}), "n_dof": res.get("n_dof")}

            if background:
                jid = _start_job("fea_static", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve())
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_modal(fixtures: list, material: Union[str, dict] = "steel", n_modes: int = 6,
                      mesh_fineness: float = 0.5, doc_id: Optional[str] = None,
                      background: bool = False) -> str:
        """Modal FEA — the natural frequencies (Hz) of the held part. Returns the first n_modes.

        fixtures : list of face queries held fixed (e.g. ["bottom"]). An unconstrained part has
                   rigid-body modes near 0 Hz, so fix it like it is mounted.
        material : name or explicit dict (density drives the mode frequencies).
        background : if True, return a job_id and solve on a worker thread — poll get_job(job_id).
        """
        try:
            d = _doc(doc_id)
            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            if not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved"})
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "mesh_order": 1, "fineness": mesh_fineness}

            def _solve() -> dict:
                res = cb.call("sim_run_modal", {"study": study, "n_modes": n_modes})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "modal solve failed")}
                return {"ok": True, "doc_id": d, "material": mat.get("name", material),
                        "freqs_hz": res.get("freqs_hz", []), "n_modes": res.get("n_modes")}

            if background:
                jid = _start_job("fea_modal", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve())
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_cfd_flow(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                     cells: int = 96, n_steps: int = 2000, doc_id: Optional[str] = None,
                     background: bool = False) -> str:
        """Wind-tunnel CFD over the current part/assembly (LBM). Returns drag, lift, Cd, Reynolds.

        CONVENTIONS (AI-CAD is Z-up): freestream is along +X; LIFT is the +Z (up) force; SPAN is Y.
        +aoa pitches the nose up (rotation about Y), so a cambered wing makes +lift. `reynolds` and
        `cd` use ref_length_mm = the part's LONGEST span (NOT chord) — for an airfoil, simulate the
        wing alone or read `ref_length_mm` and renormalise to chord yourself.

        velocity : free-stream speed (m/s).   aoa : angle of attack (degrees).
        fluid    : "air" or "water".          cells : grid pitch = domain's longest span / cells
                   (higher = finer + slower; practical 48-200).  n_steps : LBM iterations (~500-4000).
        background : if True, return a job_id immediately and solve on a worker thread (recommended
                     for cells>=120 or n_steps>=2500, which can exceed the call timeout) — then poll
                     get_job(job_id).

        RESOLUTION MATTERS: the result reports `voxel_mm`, `min_feature_mm`, and
        `min_feature_voxels` (how many cells span the thinnest part dimension). If a thin wing/tail
        edge spans < ~8 voxels it is UNDER-RESOLVED — lift especially reads near zero — and a
        `warning` is returned; raise `cells` or simulate the thin part on its own. Also reported:
        `converged` + `residual` (force settle over the last fifth of the run); a non-converged run
        is under-iterated, not physically zero.

        ⚠ ABSOLUTE Cd IS UNCALIBRATED. This is a coarse voxel LBM: the staircased surface
        over-counts force (more so on curved/angled faces), so the magnitude of drag_n/cd can be
        off by an order of magnitude and is resolution-dependent. Use it to COMPARE configurations
        and read TRENDS (does +aoa add lift? does a fillet cut drag? A vs B?), which it now gets
        right (drag scales as V^2, Cd is velocity-independent). For a TRUSTWORTHY absolute Cd/Cl,
        use run_cfd_accurate() (OpenFOAM RANS — validated, but minutes per run). Treat these LBM
        numbers as a fast directional estimate, not a certified coefficient.
        """
        try:
            d = _doc(doc_id)

            def _solve() -> dict:
                res = cb.call("sim_run_flow", {"doc_id": d, "velocity": velocity, "aoa": aoa,
                                               "fluid": fluid, "cells": cells, "n_steps": n_steps})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "flow sim failed")}
                out = {"ok": True, "doc_id": d}
                for k in ("drag_n", "lift_n", "cd", "frontal_area_mm2", "reynolds",
                          "ref_length_mm", "velocity_ms", "aoa_deg", "fluid", "grid",
                          "converged", "residual", "voxel_mm", "min_feature_mm",
                          "min_feature_voxels", "steps_run", "diverged", "warning"):
                    if k in res:
                        out[k] = res[k]
                return out

            if background:
                jid = _start_job("cfd_flow", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve())
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_cfd_accurate(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                         preset: str = "coarse", doc_id: Optional[str] = None) -> str:
        """HIGH-FIDELITY external-aero CFD via OpenFOAM (steady RANS, k-omega SST + prism layers).

        Use this when you need a TRUSTWORTHY absolute Cd/Cl, not the coarse directional estimate
        from run_cfd_flow (the built-in LBM). This is the validated path — sphere/cube land within
        ~10-15% of experiment. It body-fits a snappyHexMesh with boundary layers and solves to
        convergence, so it takes MINUTES and requires OpenFOAM installed (WSL openfoam2412 or native
        blueCFD). It ALWAYS runs in the background: returns a job_id immediately; poll get_job(job_id)
        (which reports live meshing/solve progress) for the result.

        CONVENTIONS match run_cfd_flow (Z-up): freestream +X, lift +Z, +aoa pitches nose up.
        velocity : m/s.   aoa : degrees.   fluid : "air" | "water".
        preset   : "coarse" (fastest, ~600 iters) | "medium" | "fine" | "xfine" (finest/slowest).
                   Start coarse to sanity-check, refine if you need the digits.

        Result (via get_job): {cd, cl, ld (lift/drag), drag_n, lift_n, reynolds, ref_area_mm2,
        vmax_mps, converged, iters, notes}. If `converged` is false, raise the preset / iterations.
        """
        try:
            from engine.sim.foam_runner import capabilities
            caps = capabilities()
            if not caps.get("openfoam"):
                return _dumps({"ok": False, "error":
                               "OpenFOAM is not available on this machine. Provision it with "
                               "`python -m tools.provision_openfoam --install` (WSL) or install "
                               "blueCFD-Core natively. The coarse LBM run_cfd_flow still works."})
            if preset not in ("coarse", "medium", "fine", "xfine"):
                return _dumps({"ok": False, "error": f"unknown preset {preset!r} — use "
                               "coarse|medium|fine|xfine"})
            d = _doc(doc_id)
            # OpenFOAM meshes a watertight STL surface (build_case handles mm -> m).
            stl = _tmp(".stl")
            cb.call("export_file", {"doc_id": d, "path": stl, "format": "stl"})
            fluid_dict = ({"nu": 1.0e-6, "rho": 1000.0} if fluid == "water"
                          else {"nu": 1.5e-5, "rho": 1.225})

            prog: dict = {"pct": 0, "stage": "start", "text": "queued"}

            def _on_progress(ev: dict) -> None:
                prog.update(pct=ev.get("pct", 0), stage=ev.get("stage", ""),
                            text=ev.get("text", ""))

            def _solve() -> dict:
                from engine.sim.openfoam import run_openfoam_tunnel
                r = run_openfoam_tunnel(stl, direction=(1.0, 0.0, 0.0),
                                        speed_mps=float(velocity), preset=preset,
                                        aoa_deg=float(aoa), fluid=fluid_dict,
                                        on_progress=_on_progress)
                return {"ok": True, "doc_id": d,
                        "solver": f"OpenFOAM RANS (k-omega SST, {caps.get('openfoam_kind')})",
                        "preset": preset, "cd": r.cd, "cl": r.cl, "ld": r.ld,
                        "drag_n": r.drag_n, "lift_n": r.lift_n, "side_n": r.side_n,
                        "reynolds": r.reynolds, "ref_area_mm2": r.ref_area_mm2,
                        "vmax_mps": r.vmax_mps, "converged": (not r.diverged),
                        "iters": r.steps_run, "notes": r.solver_notes,
                        "velocity_ms": float(velocity), "aoa_deg": float(aoa), "fluid": fluid}

            jid = _start_job("cfd_openfoam", _solve, progress=prog)
            return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                           "solver": caps.get("openfoam_version"), "preset": preset,
                           "note": "OpenFOAM RANS runs for minutes — poll get_job(job_id); it "
                                   "reports live meshing/solve progress."})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def get_job(job_id: str) -> str:
        """Poll a background job started by run_cfd_flow/run_fea_*(background=True).

        Returns {status: "running"|"done"|"error"}; when done, the solve `result` is included.
        Polling a long solve this way never trips the per-call transport timeout that would
        otherwise drop the whole session.
        """
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return _dumps({"ok": False, "error": f"unknown job_id {job_id!r}"})
            snap = dict(job)
        out = {"ok": True, "job_id": job_id, "status": snap["status"], "kind": snap["kind"]}
        if snap["status"] == "running" and snap.get("progress"):
            out["progress"] = snap["progress"]  # live {pct, stage, text} for long OpenFOAM runs
        if snap["status"] == "done":
            out["result"] = snap["result"]
        elif snap["status"] == "error":
            out["ok"] = False
            out["error"] = snap["error"]
        return _dumps(out)

    @mcp.tool()
    def recover_parts() -> str:
        """Reopen any auto-saved parts after a server restart (recover a dropped session).

        Every successful add_features auto-persists the part to a recoverable .step+; if the
        server was restarted (e.g. a client timeout killed it) the in-memory docs are gone — call
        this to reload them (each gets a fresh doc_id). list_parts() then shows them.
        """
        try:
            recovered = recover_autosaved()
            return _dumps({"ok": True, "recovered": recovered, "count": len(recovered)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- drawings & parts

    @mcp.tool()
    def make_drawing(doc_id: Optional[str] = None) -> str:
        """Create a 2D engineering drawing of the current part (standard views + auto-dims +
        hole callouts + title block). Returns the new drawing document id. Export it with
        export_part is NOT used here — use the drawing tools or export to DXF/PDF separately."""
        try:
            d = _doc(doc_id)
            tree = cb.call("doc_tree", {"doc_id": d})
            name = (tree.get("tree") or {}).get("name", "") if isinstance(tree.get("tree"), dict) else ""
            spec = {"views": ["front", "top", "right", "iso"],
                    "model_items": True, "hole_callouts": True}
            if name:
                spec["title"] = {"title": name}
            r = cb.call("drawing_make", {"source_doc_id": d, "spec": spec})
            return _dumps({"ok": True, "source_doc_id": d,
                           "drawing_doc_id": r.get("doc_id"), "name": name})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_standard_parts() -> str:
        """List the parametric standard-parts catalog (screws, nuts, washers, bearings, ...):
        each family's key, sizes, and lengths. Feed a family + size to make_standard_part."""
        try:
            return _dumps(cb.call("parts_catalog_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def make_standard_part(family: str, size: str, length_mm: Optional[float] = None,
                           name: Optional[str] = None, options: Optional[dict] = None) -> str:
        """Build a standard hardware part as a NEW editable part doc from the catalog.

        family : a catalog key from list_standard_parts (e.g. "shcs", "hex_nut", "washer").
        size   : a size string for that family (e.g. "M5"). length_mm where the family needs it.
        Returns the new part's doc_id (and makes it current) so you can drop it into an assembly
        or export it.
        """
        try:
            params: dict = {"family": family, "size": size}
            if length_mm is not None:
                params["length_mm"] = length_mm
            if name:
                params["name"] = name
            if options:
                params["options"] = options
            r = cb.call("parts_make", params)
            if not r.get("ok"):
                return _dumps({"ok": False, "error": r.get("error", "parts_make failed")})
            _CUR["doc_id"] = r.get("doc_id")
            _publish_current(r.get("doc_id"))
            _autosave(r.get("doc_id"))
            return _dumps({"ok": True, "doc_id": r.get("doc_id"), "name": r.get("name"),
                           "meta": r.get("meta"), "volume": r.get("volume")})
        except Exception as e:  # noqa: BLE001
            return _err(e)
