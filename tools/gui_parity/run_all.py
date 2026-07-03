#!/usr/bin/env python3
"""
tools/gui_parity/run_all.py — the five T-G9 host↔AI-CAD2 parity diff harnesses.

  gcode    scenario `cam`     : request-parity of the CAM slice + per-binary core_bridge
                                replay -> cam_post across ALL dialects -> byte-diff .nc
  netlist  scenario `ecad`    : request-parity of the ECAD slice + netlist/ERC JSON diff
                                from the captured compile responses
  dxfpdf   scenario `drawing` : request-parity of the drawing slice + per-binary replay
                                -> export_drawing dxf+pdf -> byte-diff (volatile PDF
                                metadata scrubbed, scrubs recorded)
  study    scenario `fea`     : request-parity of the sim slice + study JSON diff from
                                captured responses (solver timings scrubbed)
  sketch   scenario `sketch`  : request+response parity of the sketch solver op-batch
                                (sketch_add_entity/constraint/set_dim/drag/solve)

Usage:
  python3 tools/gui_parity/run_all.py                 # all five, capture + diff
  python3 tools/gui_parity/run_all.py gcode sketch    # a subset
  python3 tools/gui_parity/run_all.py --no-capture    # rediff existing traces

Exit 0 = all requested harnesses PASS; 1 = any FAIL (the diff is the finding).
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DOC_METHODS = ("doc_new", "doc_apply_ops", "doc_rebuild")

# Pure catalog reads: no params beyond {}, no state touched, identical results whenever
# called — the two shells legitimately poll them at different moments (host at panel
# init, AI-CAD2 on first draw after job adoption). Compared as a sorted multiset instead
# of position-in-sequence; content differences still FAIL.
READONLY_CATALOG = {"cam_post_dialects", "cam_tool_types", "cam_op_types", "cam_materials",
                    "agent_list_models", "drawing_get", "cam_tree"}


def canon_ids(lines: list[str]) -> list[str]:
    """Canonicalize engine-minted ids (doc-cpp..., cam-...) by order of first appearance,
    so an extra shell-init doc on one binary doesn't shift every later id."""
    mapping: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        tok = m.group(0)
        if tok not in mapping:
            mapping[tok] = f"<{m.group(1)}#{sum(1 for k in mapping.values() if k.startswith('<'+m.group(1)))+1}>"
        return mapping[tok]

    rx = re.compile(r"\b(doc-cpp|doc-ecad|cam-doc|drawing-doc|study)[A-Za-z0-9_-]*")
    return [rx.sub(repl, l) for l in lines]


def doc_slice(stream: list[dict], anchor_methods: tuple[str, ...],
              domain_prefixes: tuple[str, ...]) -> list[dict]:
    """The domain slice: every RPC whose method matches domain_prefixes, plus the doc
    lifecycle RPCs (doc_new/apply_ops/rebuild) for the doc ids the domain RPCs touch
    (backward slice — shell-init docs on either binary are excluded)."""
    touched: set[str] = set()
    for rec in stream:
        if any(rec.get("method", "").startswith(p) for p in anchor_methods):
            for k in ("doc_id", "part_doc_id", "source_doc_id"):
                v = rec.get("params", {}).get(k) or (rec.get("result") or {}).get(k)
                if v:
                    touched.add(v)
    out = []
    for rec in stream:
        m = rec.get("method", "")
        if any(m.startswith(p) for p in domain_prefixes):
            out.append(rec)
        elif m in DOC_METHODS:
            pid = rec.get("params", {}).get("doc_id") or (rec.get("result") or {}).get("doc_id")
            if pid in touched:
                out.append(rec)
    return out


def norm_requests(recs: list[dict], extra_volatile=frozenset()) -> list[str]:
    """Ordered mutating/stateful sequence + sorted read-only catalog multiset."""
    ordered, catalog = [], []
    for r in recs:
        line = json.dumps({"method": r["method"],
                           "params": common._scrub(r.get("params", {}), extra_volatile)},
                          sort_keys=True)
        (catalog if r["method"] in READONLY_CATALOG else ordered).append(line)
    return canon_ids(ordered) + ["# catalog reads (order-free, deduped):"] + sorted(set(canon_ids(catalog)))


def norm_responses(recs: list[dict], methods: set[str], extra_volatile=frozenset()) -> list[str]:
    lines = []
    for r in recs:
        if r.get("method") in methods:
            body = r.get("result") if r.get("ok") else {"err": r.get("err")}
            lines.append(json.dumps({"method": r["method"], "ok": r.get("ok"),
                                     "response": common._scrub(body, extra_volatile)},
                                    sort_keys=True))
    return canon_ids(lines)


def shell_extras_note(host: list[dict], new: list[dict], sliced_h: list[dict],
                      sliced_n: list[dict]) -> str:
    eh = len(host) - len(sliced_h)
    en = len(new) - len(sliced_n)
    return f"shell-level RPCs outside the domain slice: host={eh}, aicad2={en} (informational)"


# ---- replay helpers ---------------------------------------------------------------------------


def replay_and_post(cb, slice_recs: list[dict], out_dir: str, tag: str) -> dict[str, str]:
    """Replay a binary's captured CAM slice through core_bridge, then post across every
    dialect. Returns {dialect: path-to-.nc}."""
    os.makedirs(out_dir, exist_ok=True)
    idmap: dict[str, str] = {}

    def remap(o):
        if isinstance(o, dict):
            return {k: remap(v) for k, v in o.items()}
        if isinstance(o, list):
            return [remap(v) for v in o]
        if isinstance(o, str) and o in idmap:
            return idmap[o]
        return o

    cam_doc = None
    for rec in slice_recs:
        m = rec["method"]
        if m in ("doc_new", "doc_apply_ops", "cam_program"):
            res = cb.call(m, remap(rec.get("params", {})))
            for k in ("doc_id", "cam_doc_id"):
                old = (rec.get("result") or {}).get(k)
                new = (res or {}).get(k)
                if old and new:
                    idmap[old] = new
            if m == "cam_program":
                cam_doc = (res or {}).get("cam_doc_id")
    if not cam_doc:
        raise RuntimeError(f"{tag}: replay produced no cam_doc_id")
    cb.call("cam_generate", {"doc_id": cam_doc})
    dialects = [d["id"] for d in cb.call("cam_post_dialects", {}).get("dialects", [])]
    out = {}
    for d in dialects:
        r = cb.call("cam_post", {"doc_id": cam_doc, "dialect": d})
        nc = os.path.join(out_dir, f"{tag}.{d}.nc")
        with open(nc, "w") as fh:
            fh.write(r.get("gcode", ""))
        out[d] = nc
    return out


def replay_drawing_and_export(cb, slice_recs: list[dict], out_dir: str, tag: str) -> dict[str, str]:
    """Replay a binary's captured drawing slice, then export DXF + PDF."""
    os.makedirs(out_dir, exist_ok=True)
    idmap: dict[str, str] = {}

    def remap(o):
        if isinstance(o, dict):
            return {k: remap(v) for k, v in o.items()}
        if isinstance(o, list):
            return [remap(v) for v in o]
        if isinstance(o, str) and o in idmap:
            return idmap[o]
        return o

    drawing_doc = None
    for rec in slice_recs:
        m = rec["method"]
        if m in ("doc_new", "doc_apply_ops", "drawing_new", "drawing_add_view",
                 "drawing_add_dim", "drawing_add_section", "drawing_add_detail"):
            res = cb.call(m, remap(rec.get("params", {})))
            for k in ("doc_id", "drawing_doc_id"):
                old = (rec.get("result") or {}).get(k)
                new = (res or {}).get(k)
                if old and new:
                    idmap[old] = new
            if m == "drawing_new":
                drawing_doc = (res or {}).get("doc_id") or (res or {}).get("drawing_doc_id")
    if not drawing_doc:
        raise RuntimeError(f"{tag}: replay produced no drawing doc")
    out = {}
    for fmt in ("dxf", "pdf"):
        path = os.path.join(out_dir, f"{tag}.{fmt}")
        cb.call("export_drawing", {"doc_id": drawing_doc, "path": path, "format": fmt})
        out[fmt] = path
    return out


PDF_VOLATILE = [
    (re.compile(rb"/CreationDate\s*\([^)]*\)"), b"/CreationDate(scrubbed)"),
    (re.compile(rb"/ModDate\s*\([^)]*\)"), b"/ModDate(scrubbed)"),
    (re.compile(rb"/ID\s*\[[^\]]*\]"), b"/ID[scrubbed]"),
]


def diff_artifact(name: str, path_a: str, path_b: str, scrub_pdf: bool = False) -> dict:
    try:
        a = open(path_a, "rb").read()
        b = open(path_b, "rb").read()
    except OSError as e:
        return {"name": name, "pass": False, "diff": f"missing artifact: {e}"}
    scrubbed = []
    if scrub_pdf:
        for rx, rep in PDF_VOLATILE:
            if rx.search(a) or rx.search(b):
                scrubbed.append(rep.decode(errors="replace"))
            a = rx.sub(rep, a)
            b = rx.sub(rep, b)
    ok = a == b
    note = f"{len(a)}B vs {len(b)}B" + (f"; scrubbed: {scrubbed}" if scrubbed else "")
    d = ""
    if not ok:
        try:
            d = common.unified_diff(a.decode(errors="replace").splitlines(),
                                    b.decode(errors="replace").splitlines(),
                                    path_a, path_b)
        except Exception:
            d = "(binary diff)"
    return {"name": name, "pass": ok, "note": note, "diff": d}


# ---- the five harnesses -------------------------------------------------------------------------


def harness_gcode(capture: bool) -> list[dict]:
    streams = {}
    for b in ("host", "aicad2"):
        trace = (common.run_scenario(b, "cam", 300) if capture
                 else os.path.join(common.SCRATCH, b, "cam.rpc.jsonl"))
        streams[b] = common.load_stream(trace)
    slices = {b: doc_slice(s, ("cam_",), ("cam_",)) for b, s in streams.items()}
    results = [dict(common.diff_streams("gcode/requests",
                                        norm_requests(slices["host"]),
                                        norm_requests(slices["aicad2"])),
                    note=shell_extras_note(streams["host"], streams["aicad2"],
                                           slices["host"], slices["aicad2"]))]
    cb = common.bridge()
    arts = {b: replay_and_post(cb, slices[b], os.path.join(common.SCRATCH, "artifacts"), b)
            for b in ("host", "aicad2")}
    shared = sorted(set(arts["host"]) & set(arts["aicad2"]))
    missing = sorted(set(arts["host"]) ^ set(arts["aicad2"]))
    if missing:
        results.append({"name": "gcode/dialect-list", "pass": False,
                        "diff": f"dialect sets differ: {missing}"})
    for d in shared:
        results.append(diff_artifact(f"gcode/{d}.nc", arts["host"][d], arts["aicad2"][d]))
    return results


def harness_netlist(capture: bool) -> list[dict]:
    streams = {}
    for b in ("host", "aicad2"):
        trace = (common.run_scenario(b, "ecad", 300) if capture
                 else os.path.join(common.SCRATCH, b, "ecad.rpc.jsonl"))
        streams[b] = common.load_stream(trace)
    slices = {b: doc_slice(s, ("ecad_",), ("ecad_",)) for b, s in streams.items()}
    results = [dict(common.diff_streams("netlist/requests",
                                        norm_requests(slices["host"]),
                                        norm_requests(slices["aicad2"])),
                    note=shell_extras_note(streams["host"], streams["aicad2"],
                                           slices["host"], slices["aicad2"]))]

    def compile_objs(recs):
        out = []
        for r in recs:
            res = r.get("result") or {}
            comp = res.get("compile") if isinstance(res, dict) else None
            if isinstance(comp, dict) and ("netlist" in comp or "erc" in comp):
                out.append(json.dumps({"method": r["method"],
                                       "netlist": common._scrub(comp.get("netlist")),
                                       "erc": common._scrub(comp.get("erc"))},
                                      sort_keys=True))
        return canon_ids(out)

    results.append(common.diff_streams("netlist+erc/compile-json",
                                       compile_objs(slices["host"]),
                                       compile_objs(slices["aicad2"])))
    return results


def harness_dxfpdf(capture: bool) -> list[dict]:
    streams = {}
    for b in ("host", "aicad2"):
        trace = (common.run_scenario(b, "drawing", 300) if capture
                 else os.path.join(common.SCRATCH, b, "drawing.rpc.jsonl"))
        streams[b] = common.load_stream(trace)
    slices = {b: doc_slice(s, ("drawing_",), ("drawing_", "export_drawing"))
              for b, s in streams.items()}
    results = [dict(common.diff_streams("dxfpdf/requests",
                                        norm_requests(slices["host"]),
                                        norm_requests(slices["aicad2"])),
                    note=shell_extras_note(streams["host"], streams["aicad2"],
                                           slices["host"], slices["aicad2"]))]
    cb = common.bridge()
    arts = {b: replay_drawing_and_export(cb, slices[b],
                                         os.path.join(common.SCRATCH, "artifacts"), b)
            for b in ("host", "aicad2")}
    results.append(diff_artifact("dxfpdf/drawing.dxf", arts["host"]["dxf"], arts["aicad2"]["dxf"]))
    results.append(diff_artifact("dxfpdf/drawing.pdf", arts["host"]["pdf"], arts["aicad2"]["pdf"],
                                 scrub_pdf=True))
    return results


STUDY_VOLATILE = {"solve_time_s", "solve_ms", "wall_s", "runtime_s", "started", "finished",
                  "timestamp", "case_dir", "workdir", "log"}


def harness_study(capture: bool) -> list[dict]:
    streams = {}
    for b in ("host", "aicad2"):
        trace = (common.run_scenario(b, "fea", 420) if capture
                 else os.path.join(common.SCRATCH, b, "fea.rpc.jsonl"))
        streams[b] = common.load_stream(trace)
    prefixes = ("study_", "sim_", "fea_", "mesh_")
    slices = {b: doc_slice(s, prefixes, prefixes) for b, s in streams.items()}
    results = [dict(common.diff_streams("study/requests",
                                        norm_requests(slices["host"], STUDY_VOLATILE),
                                        norm_requests(slices["aicad2"], STUDY_VOLATILE)),
                    note=shell_extras_note(streams["host"], streams["aicad2"],
                                           slices["host"], slices["aicad2"]))]
    methods = {r["method"] for r in slices["host"]} | {r["method"] for r in slices["aicad2"]}
    results.append(common.diff_streams(
        "study/response-json",
        norm_responses(slices["host"], methods, STUDY_VOLATILE),
        norm_responses(slices["aicad2"], methods, STUDY_VOLATILE)))
    return results


def harness_sketch(capture: bool) -> list[dict]:
    streams = {}
    for b in ("host", "aicad2"):
        trace = (common.run_scenario(b, "sketch", 300) if capture
                 else os.path.join(common.SCRATCH, b, "sketch.rpc.jsonl"))
        streams[b] = common.load_stream(trace)
    prefixes = ("sketch_",)
    slices = {}
    for b, s in streams.items():
        sk = [r for r in s if r.get("method", "").startswith(prefixes)
              or (r.get("method") == "doc_apply_ops"
                  and any(op.get("op", "").startswith("sketch_")
                          for op in r.get("params", {}).get("ops", [])
                          if isinstance(op, dict)))]
        slices[b] = sk
    results = [dict(common.diff_streams("sketch/op-batch-requests",
                                        norm_requests(slices["host"]),
                                        norm_requests(slices["aicad2"])),
                    note=shell_extras_note(streams["host"], streams["aicad2"],
                                           slices["host"], slices["aicad2"]))]
    methods = {"sketch_solve", "doc_apply_ops"}
    results.append(common.diff_streams(
        "sketch/solver-responses",
        norm_responses(slices["host"], methods),
        norm_responses(slices["aicad2"], methods)))
    return results


HARNESSES = {
    "gcode": harness_gcode,
    "netlist": harness_netlist,
    "dxfpdf": harness_dxfpdf,
    "study": harness_study,
    "sketch": harness_sketch,
}


def main(argv: list[str]) -> int:
    capture = "--no-capture" not in argv
    names = [a for a in argv if not a.startswith("--")] or list(HARNESSES)
    os.makedirs(common.SCRATCH, exist_ok=True)
    report = os.path.join(common.SCRATCH, "report.txt")
    if os.path.exists(report):
        os.unlink(report)
    all_ok = True
    for n in names:
        try:
            results = HARNESSES[n](capture)
        except Exception as e:  # a harness crash is a FAIL with the reason, never silence
            results = [{"name": f"{n}/harness", "pass": False, "diff": f"harness error: {e}"}]
        ok = common.emit(results, f"parity harness: {n}")
        all_ok = all_ok and ok
    print(f"\n== OVERALL: {'PASS' if all_ok else 'FAIL'} ({', '.join(names)}) ==")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
