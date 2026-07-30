"""Scoring, classification and diffing — the whole judgement layer, with NO live model
and NO engine in it.

Everything here is a pure function of (recorded turns, probed artifact facts). That is
deliberate: it is the part of the benchmark that can be unit-tested against canned data
(``test_scoring.py``), so a change to the SCORE can never hide behind "the model was
having a bad day".

``score_run`` consumes:
    runs   [{prompt_id, prompt, success, stop_reason, calls:[{tool,args,ok,result}], ...}]
    parts  {prompt_id: [part_fact, ...]}      from probe.py (or canned in tests)
    files  {prompt_id: [file_fact, ...]}      from probe.py (or canned in tests)

A part_fact is exactly what the engine answered:
    {"path": str, "watertight": bool, "free_edges": int, "non_manifold_edges": int,
     "volume_mm3": float, "bbox_size_mm": [x, y, z], "bodies": int}
A file_fact: {"path": str, "name": str, "bytes": int, "pages": int|None}
"""
from __future__ import annotations

import fnmatch
import json
import math
import re
from typing import Any, Iterable, Sequence

from .prompts import PROMPTS, Prompt, criterion_label

# --------------------------------------------------------------------------------------
# What this benchmark does NOT measure. Printed with every report on purpose: the single
# aggregate percentage is worth less than the list of things it cannot see.
# --------------------------------------------------------------------------------------
LIMITS = """\
THIS BENCHMARK DOES NOT MEASURE:
 1. Correctness of anything the model SAYS. Prose is never scored. A run can score 100%
    while the chat reply misstates every number it just computed correctly.
 2. Physical/engineering QUALITY. A watertight solid of the right volume can still be a
    terrible part (wrong material, unmanufacturable, absurd wall transitions).
 3. Absolute FEA/thermal accuracy. The simulation bands are wide enough to admit any
    plausible mesh; they catch a wrong ANSWER, not a convergence error.
 4. Drawing and ECAD artifacts INDEPENDENTLY. Drawing docs (dwg-cpp*) and ECAD docs
    (ecadcpp*) are engine-private id spaces that doc_save refuses, so those criteria read
    the engine payload recorded in the turn's tool ledger. That is the tool's own JSON,
    not model prose - but it is not re-derived in a fresh process the way part geometry
    is. A defect that makes the engine return a wrong count would not be caught here.
 5. Whether a PASS was efficient. Token cost, wall time and call count are recorded but
    not scored; a prompt that flails for 40 calls and lands it still passes.
 6. Anything outside the fixed prompt set. It is a small deliberate sample of one
    discipline slice each; it is not a coverage measure of the ~418 registered tools.
 7. Model-to-model or provider-to-provider comparison, unless the compared runs used the
    SAME model, effort, provider and engine build. The report records all four so a
    comparison across different ones can be rejected.
 8. Variance. One run per prompt. A prompt that passes 60% of the time reads as a coin
    flip between two runs; treat a single-prompt flip as noise and the aggregate as the
    signal only when the delta is larger than a couple of prompts.
"""

# --------------------------------------------------------------------------------------
# Failure classification vocabulary
# --------------------------------------------------------------------------------------
#: The harness's own fault: a call that a correct harness would have accepted, or an
#: internal fault. These are the ONLY ones we can fix by editing this repo.
_HARNESS_SIGS: tuple[tuple[str, str], ...] = (
    (r"unexpected keyword argument", "unexpected_kwarg"),
    (r"got multiple values for", "bad_signature"),
    (r"unknown (?:argument|parameter|field)", "unknown_argument"),
    (r"unknown method", "unknown_rpc"),
    (r"(?:no such|unknown|unrecognized) tool", "unknown_tool"),
    (r"Traceback \(most recent call last\)", "traceback"),
    (r"\b(?:TypeError|KeyError|AttributeError|IndexError|ValueError: invalid literal)\b",
     "python_exception"),
    (r"internal (?:error|failure)", "internal_error"),
    (r"segmentation fault|SIGSEGV|worker closed", "worker_death"),
    (r"is not JSON serializable", "serialization"),
    (r"outside_workspace_root", "jail_misconfig"),
)

#: A legitimate decline: the capability genuinely is not there, or the request is
#: unanswerable as asked. A correct refusal is a WIN for the harness, and lumping it in
#: with the failure rate is the single most misleading thing the raw rate does.
_REFUSAL_SIGS: tuple[tuple[str, str], ...] = (
    (r"\bREFUSED\b", "explicit_refusal"),
    (r"not implemented|unimplemented", "not_implemented"),
    (r"no (?:such )?capability|this (?:kernel|engine) (?:exposes|has) no", "no_capability"),
    (r"not (?:supported|available) (?:by|in|on) this", "unsupported_here"),
    (r"are (?:REFUSED|refused), not performed", "refused_not_performed"),
    (r"cannot be (?:done|performed|computed) ", "cannot_perform"),
    (r"requires (?:an explicit|a licensed|a solver)", "missing_prerequisite"),
    (r"solver (?:not|is not) (?:installed|available)", "solver_absent"),
)

#: Keys whose presence means the error TAUGHT: it did not just say no, it named the fix.
_TEACHING_KEYS = ("hint", "hints", "next_actions", "did_you_mean", "suggestion",
                  "suggestions", "correction", "example", "expected", "valid",
                  "valid_values", "allowed", "options", "choices", "available")
_TEACHING_TEXT = re.compile(
    r"did you mean|looked for|try |use \w+\(|must be one of|expected one of|valid \w+ are|"
    r"instead(?: of)?[,:]? |spelled|call \w+ first|available \w+:", re.I)

#: Turn-level stop reasons that are the harness failing, not the model.
_INFRA_STOPS = {"sweep_timeout", "bench_timeout", "no_turn_in_output", "worker_closed",
                "provider_error", "provider_auth", "worker_crash", "worker_timeout",
                "engine_error"}

CLASS_HARNESS = "harness_defect"
CLASS_REFUSAL = "correct_refusal"
CLASS_MODEL = "model_error"


# --------------------------------------------------------------------------------------
# payload plumbing
# --------------------------------------------------------------------------------------
def payload(call: dict) -> Any:
    """The tool's result as structured data. Tool results reach the ledger as a JSON
    STRING about half the time (``_dumps`` at the tool boundary) and as a dict the other
    half; a scorer that only handles one silently scores zero on the other."""
    res = call.get("result")
    for _ in range(3):
        if isinstance(res, str):
            s = res.strip()
            if not s or s[0] not in "{[":
                break
            try:
                res = json.loads(s)
            except json.JSONDecodeError:
                break
        else:
            break
    return res


def _walk(node: Any) -> Iterable[Any]:
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk(v)


def deep_values(node: Any, keys: Sequence[str]) -> list[tuple[str, Any]]:
    """Every (key, value) anywhere in the payload whose key is one of *keys*."""
    want = {k.lower() for k in keys}
    out: list[tuple[str, Any]] = []
    for n in _walk(node):
        if isinstance(n, dict):
            for k, v in n.items():
                if isinstance(k, str) and k.lower() in want:
                    out.append((k, v))
    return out


def deep_path(node: Any, dotted: str) -> list[Any]:
    """Every value reachable at *dotted* (e.g. ``self_intersection.checked``) anywhere."""
    head, _, rest = dotted.partition(".")
    out: list[Any] = []
    for _k, v in deep_values(node, [head]):
        if not rest:
            out.append(v)
        else:
            out.extend(deep_path(v, rest))
    return out


def text_blob(node: Any) -> str:
    return json.dumps(node, default=str) if not isinstance(node, str) else node


_VIEW_ID = re.compile(r"^v\d+$")


def view_kind(v: dict) -> str:
    """What KIND of view the engine says this is. Real payloads spell it three ways:
    ``type`` ("base"/"projected"/"section"/"iso"), ``orientation`` ("front"/"top") when
    the tool reports projection direction instead, and a bare orientation string in the
    ``views`` map. Only reading ``type`` under-counts and would fail a correct drawing."""
    for k in ("type", "view_type", "kind", "orientation"):
        val = v.get(k)
        if isinstance(val, str) and val:
            return val.lower()
    return "?"


def collect_views(node: Any) -> list[dict]:
    """Drawing views the ENGINE reported, deduplicated by view_id so re-reading the same
    sheet cannot inflate the count."""
    keyed: dict[str, dict] = {}
    loose: list[dict] = []
    for n in _walk(node):
        if not isinstance(n, dict):
            continue
        # make_drawing answers with {"views": {"v1": "front", "v2": "top", ...}} — a map,
        # not a list of view dicts. Without this branch a correct 4-view drawing reads as 0.
        vmap = n.get("views")
        if isinstance(vmap, dict) and vmap and all(
                isinstance(k, str) and _VIEW_ID.match(k) and isinstance(v, str)
                for k, v in vmap.items()):
            for vid, orient in vmap.items():
                keyed.setdefault(vid, {"view_id": vid, "orientation": orient})
        if n.get("view_id"):
            prior = keyed.get(str(n["view_id"]))
            if prior is None or view_kind(prior) == "?":
                keyed[str(n["view_id"])] = n
        elif n.get("id") and ("annotations" in n or "orientation" in n):
            loose.append(n)
    return list(keyed.values()) or loose


def _ok_calls(run: dict, tools: Sequence[str]) -> list[dict]:
    """Successful calls to any accepted spelling. A FAILED call cannot satisfy a
    criterion — its payload is an error, not an engine measurement."""
    want = {t.lower() for t in tools}
    return [c for c in (run.get("calls") or [])
            if c.get("ok") and str(c.get("tool", "")).lower() in want]


# --------------------------------------------------------------------------------------
# unit interpretation
# --------------------------------------------------------------------------------------
_UNIT_SCALES: dict[str, tuple[tuple[str, float], ...]] = {
    # (suffix in the key -> multiplier to reach the criterion's unit)
    "stress": (("_mpa", 1.0), ("_pa", 1e-6), ("_ksi", 6.89476), ("_psi", 6.89476e-3)),
    "length": (("_mm", 1.0), ("_m", 1000.0), ("_cm", 10.0), ("_in", 25.4)),
    "mass": (("_g", 1.0), ("_kg", 1000.0), ("_lb", 453.592)),
}
#: When the key carries no unit suffix the value is ambiguous, so BOTH readings are
#: tried and the report says which one landed. Never silently pick one.
_UNITLESS_ALTS: dict[str, tuple[tuple[float, str], ...]] = {
    "stress": ((1.0, "MPa"), (1e-6, "Pa->MPa")),
    "length": ((1.0, "mm"), (1000.0, "m->mm")),
    "mass": ((1.0, "g"), (1000.0, "kg->g")),
}


def interpretations(key: str, value: float, quantity: str | None) -> list[tuple[float, str]]:
    k = key.lower()
    if quantity:
        for suffix, mult in _UNIT_SCALES.get(quantity, ()):
            if k.endswith(suffix):
                return [(value * mult, suffix.lstrip("_") or "raw")]
        return [(value * m, note) for m, note in _UNITLESS_ALTS.get(quantity, ((1.0, "raw"),))]
    return [(value, "raw")]


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", v)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------------------
# criterion evaluators — each returns (passed, observed) and observed CARRIES THE VALUE
# --------------------------------------------------------------------------------------
def _best_part(parts: list[dict], c: dict) -> dict | None:
    """The probed part a criterion is about. When the criterion names a volume, pick the
    part closest to it (the turn may leave several docs on disk, including intermediates);
    otherwise the largest solid."""
    solid = [p for p in parts if (p.get("volume_mm3") or 0) > 0]
    if not solid:
        return None
    if c.get("expect_mm3"):
        return min(solid, key=lambda p: abs(float(p["volume_mm3"]) - float(c["expect_mm3"])))
    if c.get("size_mm"):
        want = [float(x) for x in c["size_mm"]]
        def err(p: dict) -> float:
            got = sorted(float(x) for x in (p.get("bbox_size_mm") or [0, 0, 0]))
            return sum(abs(a - b) for a, b in zip(got, sorted(want)))
        return min(solid, key=err)
    return max(solid, key=lambda p: float(p["volume_mm3"]))


def _eval_part_health(c: dict, run: dict, parts: list[dict], files: list[dict]):
    p = _best_part(parts, c)
    if p is None:
        return False, f"no solid part on disk (probed {len(parts)} doc(s))"
    good = (bool(p.get("watertight")) and int(p.get("free_edges") or 0) == 0
            and int(p.get("non_manifold_edges") or 0) == 0
            and float(p.get("volume_mm3") or 0) > 0)
    return good, (f"{p['path'].rsplit('/', 1)[-1]}: watertight={bool(p.get('watertight'))} "
                  f"free_edges={p.get('free_edges')} non_manifold={p.get('non_manifold_edges')} "
                  f"volume={float(p.get('volume_mm3') or 0):.1f} mm3")


def _eval_part_volume(c: dict, run: dict, parts: list[dict], files: list[dict]):
    p = _best_part(parts, c)
    if p is None:
        return False, f"no solid part on disk (probed {len(parts)} doc(s))"
    got, want = float(p["volume_mm3"]), float(c["expect_mm3"])
    err = abs(got - want) / want * 100.0
    return err <= float(c["tol_pct"]), (
        f"{p['path'].rsplit('/', 1)[-1]}: volume {got:.1f} mm3 vs expected {want:.1f} "
        f"({err:.2f}% off, tol {c['tol_pct']}%)")


def _eval_part_bbox(c: dict, run: dict, parts: list[dict], files: list[dict]):
    p = _best_part(parts, c)
    if p is None:
        return False, f"no solid part on disk (probed {len(parts)} doc(s))"
    got = sorted(round(float(x), 3) for x in (p.get("bbox_size_mm") or []))
    want = sorted(float(x) for x in c["size_mm"])
    if len(got) != len(want):
        return False, f"bbox unavailable ({got})"
    devs = [abs(a - b) for a, b in zip(got, want)]
    return max(devs) <= float(c["tol_mm"]), (
        f"{p['path'].rsplit('/', 1)[-1]}: bbox "
        + " x ".join(f"{v:g}" for v in got)
        + " mm vs " + " x ".join(f"{v:g}" for v in want)
        + f" (worst dev {max(devs):.3f} mm, tol {c['tol_mm']})")


def _eval_file_present(c: dict, run: dict, parts: list[dict], files: list[dict]):
    hits = [f for f in files if fnmatch.fnmatch(f.get("name", ""), c["glob"])]
    if not hits:
        return False, f"no file matching {c['glob']} (saw {[f.get('name') for f in files][:8]})"
    f = max(hits, key=lambda x: int(x.get("bytes") or 0))
    if c.get("min_pages") is not None:
        pages = f.get("pages")
        if pages is None:
            return False, f"{f['name']}: {f.get('bytes')} bytes but page count unreadable"
        return int(pages) >= int(c["min_pages"]), (
            f"{f['name']}: {pages} page(s), {f.get('bytes')} bytes "
            f"(need >= {c['min_pages']})")
    ok = int(f.get("bytes") or 0) >= int(c.get("min_bytes") or 1)
    return ok, f"{f['name']}: {f.get('bytes')} bytes"


def _ledger_numbers(c: dict, run: dict) -> list[tuple[str, str, float, str]]:
    """(tool, key, interpreted_value, unit_note) for every candidate in the ledger."""
    out: list[tuple[str, str, float, str]] = []
    for call in _ok_calls(run, c["tools"]):
        pay = payload(call)
        for k, v in deep_values(pay, c["keys"]):
            n = _num(v)
            if n is None:
                continue
            for val, note in interpretations(k, n, c.get("quantity")):
                out.append((str(call.get("tool")), k, val, note))
    return out


def _eval_ledger_number(c: dict, run: dict, parts: list[dict], files: list[dict]):
    cands = _ledger_numbers(c, run)
    if not cands:
        return False, (f"no engine value for {'/'.join(c['keys'])} in any successful "
                       f"{'/'.join(c['tools'][:3])}... call")
    lo, hi = float(c["min"]), float(c["max"])
    for tool, k, val, note in cands:
        if lo <= val <= hi:
            return True, f"{tool}.{k} = {val:.4g} ({note}) in [{lo:g}, {hi:g}]"
    tool, k, val, note = cands[0]
    return False, (f"{tool}.{k} = {val:.4g} ({note}) outside [{lo:g}, {hi:g}]"
                   + (f"; {len(cands) - 1} other reading(s) also out" if len(cands) > 1 else ""))


def _eval_ledger_near(c: dict, run: dict, parts: list[dict], files: list[dict]):
    cands = _ledger_numbers(dict(c, quantity=c.get("quantity")), run)
    if not cands:
        return False, f"no engine value for {'/'.join(c['keys'])} in the ledger"
    exp, tol = float(c["expect"]), float(c["tol"])
    for tool, k, val, note in cands:
        if abs(val - exp) <= tol:
            return True, f"{tool}.{k} = {val:.4g} ({note}), target {exp:g} +/-{tol:g}"
    tool, k, val, note = min(cands, key=lambda t: abs(t[2] - exp))
    return False, f"closest {tool}.{k} = {val:.4g}, target {exp:g} +/-{tol:g}"


def _eval_ledger_min(c: dict, run: dict, parts: list[dict], files: list[dict]):
    cands = _ledger_numbers(c, run)
    if not cands:
        return False, f"no engine value for {'/'.join(c['keys'])} in the ledger"
    tool, k, val, _n = max(cands, key=lambda t: t[2])
    return val >= float(c["min"]), f"max {tool}.{k} = {val:g} (need >= {c['min']})"


def _eval_ledger_zero(c: dict, run: dict, parts: list[dict], files: list[dict]):
    """Zero errors, and the check must actually have RUN: an absent count is a fail, not
    a pass by default. This is the difference between 'DRC is clean' and 'nobody ran DRC'."""
    cands = _ledger_numbers(c, run)
    if not cands:
        return False, (f"{'/'.join(c['keys'])} never reported — the check did not run "
                       f"(no successful {'/'.join(c['tools'][:2])} call)")
    tool, k, val, _n = max(cands, key=lambda t: t[2])
    return val == 0, f"worst {tool}.{k} = {val:g} (need 0)"


def _eval_ledger_false(c: dict, run: dict, parts: list[dict], files: list[dict]):
    vals: list[tuple[str, Any]] = []
    for call in _ok_calls(run, c["tools"]):
        for v in deep_path(payload(call), c["path"]):
            vals.append((str(call.get("tool")), v))
    if not vals:
        return False, f"{c['path']} never reported by the engine"
    return all(v is False for _t, v in vals), (
        f"{c['path']} = {[v for _t, v in vals]} from {sorted({t for t, _v in vals})}")


def _eval_ledger_text(c: dict, run: dict, parts: list[dict], files: list[dict]):
    needle = str(c["needle"]).lower()
    for call in _ok_calls(run, c["tools"]):
        blob = text_blob(payload(call)).lower()
        if needle in blob:
            return True, f"{call.get('tool')} echoed {c['needle']!r}"
    return False, (f"{c['needle']!r} not in any successful "
                   f"{'/'.join(c['tools'][:2])} payload")


def _eval_ledger_count(c: dict, run: dict, parts: list[dict], files: list[dict]):
    """How MANY, when the engine answers with a list rather than a count. ecad_autoroute
    reports ``routed_nets`` as the list of nets it routed, not an integer; a numeric-only
    reader finds nothing there and scores a fully routed board as unrouted."""
    best: tuple[str, str, int] | None = None
    for call in _ok_calls(run, c["tools"]):
        for k, v in deep_values(payload(call), c["keys"]):
            n = len(v) if isinstance(v, (list, tuple, dict)) else _num(v)
            if n is None:
                continue
            if best is None or int(n) > best[2]:
                best = (str(call.get("tool")), k, int(n))
    if best is None:
        return False, f"no engine value for {'/'.join(c['keys'])} in the ledger"
    tool, k, n = best
    return n >= int(c["min"]), f"{tool}.{k} counts {n} (need >= {c['min']})"


def _eval_ledger_distinct(c: dict, run: dict, parts: list[dict], files: list[dict]):
    """Distinct engine-assigned identifiers — the honest way to count placed components
    (``ref``, which the engine assigns and may reassign) or nets (``net``) when no tool
    reports a total."""
    seen: set[str] = set()
    tools: set[str] = set()
    for call in _ok_calls(run, c["tools"]):
        for _k, v in deep_values(payload(call), c["keys"]):
            if isinstance(v, str) and v:
                seen.add(v)
                tools.add(str(call.get("tool")))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item:
                        seen.add(item)
                        tools.add(str(call.get("tool")))
    return len(seen) >= int(c["min"]), (
        f"{len(seen)} distinct {'/'.join(c['keys'])} {sorted(seen)[:8]} from "
        f"{sorted(tools)} (need >= {c['min']})")


def _eval_ledger_view_count(c: dict, run: dict, parts: list[dict], files: list[dict]):
    views: dict[str, dict] = {}
    for call in _ok_calls(run, c["tools"]):
        for v in collect_views(payload(call)):
            key = str(v.get("view_id") or v.get("id") or len(views))
            if key not in views or view_kind(views[key]) == "?":
                views[key] = v
    kinds = [view_kind(v) for v in views.values()]
    return len(views) >= int(c["min"]), (
        f"{len(views)} engine view(s) {kinds} (need >= {c['min']})")


def _eval_ledger_view_type(c: dict, run: dict, parts: list[dict], files: list[dict]):
    """Is a view of this KIND on the sheet?

    Two accepted proofs, because drawing_add_section's own reply carries the minted
    view_id, the cut axis and the cut line but NO "type" field — so reading only the type
    would fail a section view the engine demonstrably created:
      1. a collected view whose kind is the required one, or
      2. a successful call to a tool whose whole job is that view kind, which came back
         with an engine-minted view_id.
    """
    want = str(c["require"]).lower()
    kinds: set[str] = set()
    for call in _ok_calls(run, c["tools"]):
        for v in collect_views(payload(call)):
            kinds.add(view_kind(v))
    if want in kinds:
        return True, f"engine view kinds {sorted(k for k in kinds if k != '?')}"
    for call in _ok_calls(run, c.get("from_tools", ())):
        vids = [v for v in deep_path(payload(call), "view_id") if v]
        if vids:
            return True, (f"{call.get('tool')} minted view_id {vids[0]!r} — engine created "
                          f"the {want} view (its reply carries no 'type' field)")
    return False, (f"engine view kinds {sorted(k for k in kinds if k != '?')} and no "
                   f"successful {'/'.join(c.get('from_tools', ('-',))[:2])} call "
                   f"(need a {want!r} view)")


_EVAL = {
    "part_health": _eval_part_health,
    "part_volume": _eval_part_volume,
    "part_bbox": _eval_part_bbox,
    "file_present": _eval_file_present,
    "ledger_number": _eval_ledger_number,
    "ledger_near": _eval_ledger_near,
    "ledger_min": _eval_ledger_min,
    "ledger_zero": _eval_ledger_zero,
    "ledger_false": _eval_ledger_false,
    "ledger_text": _eval_ledger_text,
    "ledger_count": _eval_ledger_count,
    "ledger_distinct": _eval_ledger_distinct,
    "ledger_view_count": _eval_ledger_view_count,
    "ledger_view_type": _eval_ledger_view_type,
}


def _label(c: dict) -> str:
    """A malformed criterion must still produce a row: a report that crashes on one bad
    criterion loses the other eleven prompts' worth of measurement."""
    try:
        return criterion_label(c)
    except Exception:  # noqa: BLE001
        return f"<malformed {c.get('kind')!r} criterion>"


def evaluate_criterion(c: dict, run: dict, parts: list[dict],
                       files: list[dict]) -> dict:
    fn = _EVAL.get(str(c.get("kind")))
    if fn is None:
        raise KeyError(f"unknown criterion kind {c.get('kind')!r}")
    try:
        ok, observed = fn(c, run, parts, files)
    except Exception as exc:  # noqa: BLE001 — a broken evaluator must not be a silent pass
        return {"kind": c.get("kind"), "label": _label(c), "passed": False,
                "observed": f"evaluator raised {type(exc).__name__}: {exc}"}
    return {"kind": c.get("kind"), "label": _label(c), "passed": bool(ok),
            "observed": observed}


# --------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------
def _err_text(call: dict) -> str:
    pay = payload(call)
    return text_blob(pay)


def _is_taught(call: dict) -> bool:
    pay = payload(call)
    if isinstance(pay, dict):
        for k in _TEACHING_KEYS:
            v = pay.get(k)
            if v not in (None, "", [], {}):
                return True
        for _k, v in deep_values(pay, _TEACHING_KEYS):
            if v not in (None, "", [], {}):
                return True
    return bool(_TEACHING_TEXT.search(_err_text(call)))


def classify_call(call: dict) -> tuple[str, str, bool]:
    """(bucket, signature, taught) for ONE failed call."""
    txt = _err_text(call)
    for pat, sig in _HARNESS_SIGS:
        if re.search(pat, txt, re.I):
            return CLASS_HARNESS, sig, _is_taught(call)
    for pat, sig in _REFUSAL_SIGS:
        if re.search(pat, txt, re.I):
            return CLASS_REFUSAL, sig, _is_taught(call)
    taught = _is_taught(call)
    return CLASS_MODEL, "taught_error" if taught else "untaught_error", taught


def classify_prompt(prompt: Prompt, run: dict, failed_criteria: list[dict]) -> dict:
    """Three-way verdict for a FAILING prompt.

    Precedence is deliberate: a harness defect anywhere in the turn owns the prompt, because
    it is the only class this repo can fix and it must never hide behind a model error.
    """
    bad = [c for c in (run.get("calls") or []) if not c.get("ok")]
    per_call = [classify_call(c) for c in bad]
    stop = str(run.get("stop_reason") or "")

    if stop in _INFRA_STOPS:
        return {"class": CLASS_HARNESS, "subtype": f"turn_{stop}", "taught": False,
                "why": f"turn never completed: stop_reason={stop}"}

    harness = [(c, s) for c, (b, s, _t) in zip(bad, per_call) if b == CLASS_HARNESS]
    if harness:
        sigs = sorted({s for _c, s in harness})
        return {"class": CLASS_HARNESS, "subtype": ",".join(sigs), "taught": False,
                "why": (f"{len(harness)} call(s) failed on the harness's own terms "
                        f"({', '.join(sigs)}): "
                        + "; ".join(f"{c.get('tool')}" for c, _s in harness[:4]))}

    refusals = [(c, s) for c, (b, s, _t) in zip(bad, per_call) if b == CLASS_REFUSAL]
    if refusals:
        sigs = sorted({s for _c, s in refusals})
        return {"class": CLASS_REFUSAL, "subtype": ",".join(sigs), "taught": True,
                "why": (f"the engine correctly declined ({', '.join(sigs)}) via "
                        + ", ".join(sorted({str(c.get('tool')) for c, _s in refusals}))
                        + "; the unmet criteria are unreachable by design")}

    model = [(c, s, t) for c, (b, s, t) in zip(bad, per_call) if b == CLASS_MODEL]
    if model:
        taught = any(t for _c, _s, t in model)
        return {"class": CLASS_MODEL,
                "subtype": "taught_error" if taught else "untaught_error", "taught": taught,
                "why": (f"{len(model)} model-authored bad call(s) on "
                        + ", ".join(sorted({str(c.get('tool')) for c, _s, _t in model})[:4])
                        + ("; the errors named the fix and the turn still missed the criteria"
                           if taught else
                           "; NONE of the errors named a fix - guidance gap"))}

    if prompt.refusal_ok:
        return {"class": CLASS_REFUSAL, "subtype": "declined_without_failed_call",
                "taught": True,
                "why": ("no call failed and this prompt is marked refusal_ok: the harness "
                        "declined cleanly rather than fabricating an answer")}

    return {"class": CLASS_MODEL, "subtype": "silent_shortfall", "taught": False,
            "why": ("every tool call succeeded and the artifact still misses "
                    + "; ".join(fc["label"] for fc in failed_criteria[:3])
                    + " - the harness never objected, so it taught nothing")}


# --------------------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------------------
def score_run(runs: Sequence[dict], parts: dict[str, list[dict]],
              files: dict[str, list[dict]], meta: dict | None = None,
              prompt_set: Sequence[Prompt] = PROMPTS) -> dict:
    by_id = {p.id: p for p in prompt_set}
    seen = {str(r.get("prompt_id")) for r in runs}
    results: list[dict] = []
    total_calls = failed_calls = 0

    for prompt in prompt_set:
        run = next((r for r in runs if str(r.get("prompt_id")) == prompt.id), None)
        if run is None:
            results.append({
                "prompt_id": prompt.id, "passed": False, "criteria": [],
                "criteria_passed": 0, "criteria_total": len(prompt.criteria),
                "calls": 0, "failed_calls": 0, "elapsed_s": None,
                "stop_reason": "not_run",
                "classification": {"class": CLASS_HARNESS, "subtype": "prompt_not_run",
                                   "taught": False, "why": "no turn recorded for this prompt"},
            })
            continue
        calls = run.get("calls") or []
        bad = [c for c in calls if not c.get("ok")]
        total_calls += len(calls)
        failed_calls += len(bad)

        crits = [evaluate_criterion(c, run, parts.get(prompt.id, []),
                                    files.get(prompt.id, []))
                 for c in prompt.criteria]
        n_pass = sum(1 for c in crits if c["passed"])
        passed = n_pass == len(crits) and len(crits) > 0
        row = {
            "prompt_id": prompt.id, "passed": passed, "criteria": crits,
            "criteria_passed": n_pass, "criteria_total": len(crits),
            "calls": len(calls), "failed_calls": len(bad),
            "elapsed_s": run.get("elapsed_s"),
            "stop_reason": run.get("stop_reason"),
            "turn_success": bool(run.get("success")),
        }
        if not passed:
            row["classification"] = classify_prompt(
                prompt, run, [c for c in crits if not c["passed"]])
        results.append(row)

    n = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    c_tot = sum(r["criteria_total"] for r in results)
    c_pass = sum(r["criteria_passed"] for r in results)
    cls: dict[str, int] = {CLASS_HARNESS: 0, CLASS_MODEL: 0, CLASS_REFUSAL: 0}
    taught = untaught = 0
    for r in results:
        k = r.get("classification")
        if not k:
            continue
        cls[k["class"]] = cls.get(k["class"], 0) + 1
        if k["class"] == CLASS_MODEL:
            taught += 1 if k.get("taught") else 0
            untaught += 0 if k.get("taught") else 1

    return {
        "schema": "chudware.artifact_validity/1",
        "meta": dict(meta or {}),
        "prompts": results,
        "aggregate": {
            "prompts_total": n,
            "prompts_passed": n_pass,
            "validity_pct": round(100.0 * n_pass / n, 1) if n else 0.0,
            "criteria_total": c_tot,
            "criteria_passed": c_pass,
            "criteria_pct": round(100.0 * c_pass / c_tot, 1) if c_tot else 0.0,
            "total_calls": total_calls,
            "failed_calls": failed_calls,
            "call_failure_rate_pct": (round(100.0 * failed_calls / total_calls, 1)
                                      if total_calls else 0.0),
            "classification": cls,
            "model_error_taught": taught,
            "model_error_untaught": untaught,
            "prompts_missing": sorted(set(by_id) - seen),
        },
    }


# --------------------------------------------------------------------------------------
# reporting / diffing
# --------------------------------------------------------------------------------------
def format_report(report: dict, limits: bool = True) -> str:
    a = report["aggregate"]
    m = report.get("meta") or {}
    L: list[str] = []
    L.append("=" * 86)
    L.append(f"ARTIFACT-VALIDITY BENCHMARK  tag={m.get('tag', '?')}  "
             f"model={m.get('model', '?')} effort={m.get('effort', '?')} "
             f"provider={m.get('provider', '?')}")
    L.append(f"engine={m.get('engine', '?')}  engine_mtime={m.get('engine_mtime', '?')}  "
             f"stale_sources={m.get('stale_sources', '?')}")
    L.append("=" * 86)
    L.append(f"{'prompt':<24} {'pass':<5} {'crit':<7} {'calls':<11} {'s':>5}  class")
    L.append("-" * 86)
    for r in report["prompts"]:
        k = r.get("classification") or {}
        L.append(f"{r['prompt_id']:<24} "
                 f"{'PASS' if r['passed'] else 'FAIL':<5} "
                 f"{str(r['criteria_passed']) + '/' + str(r['criteria_total']):<7} "
                 f"{str(r['calls']) + ' (' + str(r['failed_calls']) + ' bad)':<11} "
                 f"{(r.get('elapsed_s') or 0):5.0f}  "
                 f"{k.get('class', '')}"
                 + (f"/{k.get('subtype')}" if k.get("subtype") else ""))
        for c in r["criteria"]:
            L.append(f"    [{'ok' if c['passed'] else 'XX'}] {c['label']}")
            L.append(f"         -> {c['observed']}")
        if k:
            L.append(f"    WHY: {k.get('why')}")
    L.append("-" * 86)
    L.append(f"ARTIFACT VALIDITY : {a['prompts_passed']}/{a['prompts_total']} prompts "
             f"= {a['validity_pct']}%")
    L.append(f"CRITERIA          : {a['criteria_passed']}/{a['criteria_total']} "
             f"= {a['criteria_pct']}%")
    L.append(f"TOOL-CALL FAILURES: {a['failed_calls']}/{a['total_calls']} "
             f"= {a['call_failure_rate_pct']}%  "
             "(headline-misleading on its own - see classification)")
    c = a["classification"]
    L.append(f"FAILING PROMPTS BY CAUSE: harness_defect={c.get('harness_defect', 0)}  "
             f"model_error={c.get('model_error', 0)} "
             f"(taught={a['model_error_taught']}, untaught={a['model_error_untaught']})  "
             f"correct_refusal={c.get('correct_refusal', 0)}")
    if a["prompts_missing"]:
        L.append(f"NOT RUN: {a['prompts_missing']}")
    if limits:
        L.append("")
        L.append(LIMITS.rstrip())
    return "\n".join(L)


def diff_reports(base: dict, new: dict) -> str:
    """before/after against a stored baseline."""
    ba, na = base["aggregate"], new["aggregate"]
    bm, nm = base.get("meta") or {}, new.get("meta") or {}
    L: list[str] = []
    L.append("=" * 86)
    L.append(f"COMPARISON  baseline={bm.get('tag', '?')} -> current={nm.get('tag', '?')}")
    L.append("=" * 86)
    for label, key, unit in (("artifact validity", "validity_pct", "%"),
                             ("criteria passed", "criteria_pct", "%"),
                             ("tool-call failure rate", "call_failure_rate_pct", "%")):
        b, n = ba.get(key, 0), na.get(key, 0)
        L.append(f"{label:<24} {b:>7}{unit} -> {n:>7}{unit}   delta {n - b:+.1f}{unit}")
    for key in ("harness_defect", "model_error", "correct_refusal"):
        b = (ba.get("classification") or {}).get(key, 0)
        n = (na.get("classification") or {}).get(key, 0)
        L.append(f"{'failing: ' + key:<24} {b:>7}  -> {n:>7}    delta {n - b:+d}")
    bp = {r["prompt_id"]: r for r in base["prompts"]}
    np_ = {r["prompt_id"]: r for r in new["prompts"]}
    L.append("-" * 86)
    for pid in sorted(set(bp) | set(np_)):
        b, n = bp.get(pid), np_.get(pid)
        bs = "PASS" if (b and b["passed"]) else ("FAIL" if b else "absent")
        ns = "PASS" if (n and n["passed"]) else ("FAIL" if n else "absent")
        mark = "  " if bs == ns else ("++" if ns == "PASS" else "!!")
        detail = ""
        if b and n:
            detail = (f"  criteria {b['criteria_passed']}/{b['criteria_total']}"
                      f" -> {n['criteria_passed']}/{n['criteria_total']}")
        L.append(f"{mark} {pid:<24} {bs:<6} -> {ns:<6}{detail}")
    if (bm.get("model"), bm.get("effort"), bm.get("provider")) != \
       (nm.get("model"), nm.get("effort"), nm.get("provider")):
        L.append("")
        L.append("!! CONFIGURATION DIFFERS - this is NOT a harness comparison: "
                 f"{bm.get('model')}/{bm.get('effort')}/{bm.get('provider')} vs "
                 f"{nm.get('model')}/{nm.get('effort')}/{nm.get('provider')}")
    if bm.get("engine_mtime") != nm.get("engine_mtime"):
        L.append(f"note: engine build differs ({bm.get('engine_mtime')} vs "
                 f"{nm.get('engine_mtime')})")
    return "\n".join(L)
