"""aicad_drive.py - ergonomic driver for the AI-CAD *fixed* engine via core_bridge.

The live MCP server is bypassed (it was deadlocked on the old DLL); this loads the freshly
staged dist/AI-CAD/libaicad.dll directly through mcp_server.core_bridge. Run from the repo
root (PYTHONPATH=. or run a script that imports `tools.aicad_drive`), ideally with the
runtime venv python:
  %APPDATA%\\AI-CAD\\runtime\\venv\\Scripts\\python.exe

Quick use:
  import tools.aicad_drive as eng
  d = eng.new_part("demo")
  r = eng.ops(d, [eng.box("Base", 80, 50, 20)])      # r['ok'], r['volume_mm3'], r['features']
  print(eng.measure(d))                               # volume_mm3, mass_g, com, inertia, bodies
  print(eng.check(d))                                 # issues: [] for a clean solid
  print(eng.query(d, {"of": "face", "side": "top"}))
  eng.render(d, "out.png")                            # writes a PNG (open/Read it)
  eng.save(d, "out.step+")                            # native, editable
  eng.export(d, "out.step", "step")

For complex features (sketch/extrude/revolve/loft/sweep/hole/bolt_circle/pattern/mirror/
fillet/chamfer/shell/draft/transform) use eng.feat(type, name, params, refs) with the op
JSON shapes from core/tests/test_document.cpp and mcp_server/cad_tools.py / the cheatsheets.
A dimension is eng.dim(value)  ->  {"value": value, "expr": None}.
"""
from __future__ import annotations
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mcp_server import core_bridge as cb  # noqa: E402

call = cb.call
CoreError = cb.CoreError


def dim(value, expr=None):
    return {"value": value, "expr": expr}


# ---- lifecycle / evaluation ---------------------------------------------------
def new_part(name="part"):
    return call("doc_new", {"name": name})["doc_id"]


def ops(doc, op_list):
    """Apply a list of ops. Returns {ok, revision, body_count, volume_mm3, features:[...]}.
    Catches CoreError (hard rejection) and returns it as {ok:False, error, code}."""
    try:
        return call("doc_apply_ops", {"doc_id": doc, "ops": op_list})
    except CoreError as e:
        return {"ok": False, "error": str(e), "code": getattr(e, "code", ""), "_hard": True}


def measure(doc, density=7.85):
    return call("eval_mass", {"doc_id": doc, "density_g_cm3": density})


def check(doc):
    return call("eval_check", {"doc_id": doc})


def query(doc, q):
    return call("doc_query", {"doc_id": doc, "query": q})


def tree(doc):
    return call("doc_tree", {"doc_id": doc})


def undo(doc):
    return call("doc_undo", {"doc_id": doc})


def redo(doc):
    return call("doc_redo", {"doc_id": doc})


def close(doc):
    return call("doc_close", {"doc_id": doc})


# ---- output -------------------------------------------------------------------
def save(doc, path):
    """Native editable .step+."""
    return call("doc_save", {"doc_id": doc, "path": path})


def export(doc, path, fmt="step"):
    return call("export_file", {"doc_id": doc, "path": path, "format": fmt})


def render(doc, path):
    """Write an annotated PNG to `path` (open/Read it to SEE the part)."""
    return call("doc_render_png", {"doc_id": doc, "out_path": path})


# ---- op builders --------------------------------------------------------------
def feat(ftype, name, params=None, refs=None):
    f = {"type": ftype, "name": name, "params": params or {}}
    if refs:
        f["refs"] = refs
    return {"op": "add_feature", "feature": f}


def box(name, l, w, h):
    return feat("box", name, {"length": dim(l), "width": dim(w), "height": dim(h)})


def cylinder(name, r, h):
    return feat("cylinder", name, {"radius": dim(r), "height": dim(h)})


def sphere(name, r):
    return feat("sphere", name, {"radius": dim(r)})


if __name__ == "__main__":
    # smoke test: box volume should be 8000
    d = new_part("smoke")
    r = ops(d, [box("B", 20, 20, 20)])
    m = measure(d)
    print("smoke ok=%s vol=%s mass=%s" % (r.get("ok"), m.get("volume_mm3"), m.get("mass_g")))
    close(d)
