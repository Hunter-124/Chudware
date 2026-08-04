"""advisor_e2e — T-W11 Pillar-20 capstone bench.

Runs ONE fixed mission (the bearing-mount bracket) two ways —

  * **baseline**: a single linear ``general_agent`` turn
    (``mission_dispatch.run_linear_role_turn``), the shipped interactive lane;
  * **advisory**: the orchestrator role over a compiled ``aicad.dag/1`` mission
    graph with role nodes and the T-W5 foreman enabled
    (``dag.executor.run`` + ``node_runner.make_default_agent_fn``)

— across model tiers and seeds, and scores the result with DETERMINISTIC checks
only. No LLM judges anywhere: every number here comes from geometry queries,
a tolerance calculator, or the verifier battery.

Two outputs, and the second is the one that pays for the run:

1. a comparison table (loop x tier), the card's stated deliverable; and
2. a **gap ledger** — every tool failure observed live, bucketed into
   ``harness_defect`` / ``dialect_gap`` / ``guidance_gap`` / ``teaching`` with
   the verbatim args and error text needed to reproduce it offline.

Bucket (1) tells you which loop won. Bucket (2) tells you what to fix, which is
the whole point of spending real subscription quota on this.

Live legs are OFF by default. ``_run(smoke, workdir)`` — the entry the flywheel
runner calls — scores the deterministic floor and records the live rows as
``pending`` (the T-F13/T-H7 honesty posture: never fabricate a row that did not
execute). Set ``CHUDWARE_BENCH_LIVE=1`` to spend real quota; every leg is gated
by :mod:`tools.flywheel.bench.spend_guard`, which halts the matrix at an
absolute utilization cap rather than spending blind.
"""
from __future__ import annotations

import json
import os
import re
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional

from ..baseline import Baseline, pending_frontier
from ..driver import BenchDriver, BENCHES_DIR
from ..schema import BenchResult, ExampleScore

BENCH_ID = "advisor_e2e"
DATASET_ID = "advisor_e2e/bearing_mount_bracket"

# ── The mission ──────────────────────────────────────────────────────────────
# Frozen so every leg is byte-identical. Numbers are the scoring contract:
# a 20 g6 shaft in a sliding fit, four M5 clearance holes on a rectangular
# pattern, aluminium, and an FEA sanity check under a 500 N side load.

SHAFT_NOMINAL_MM = 20.0
SHAFT_TOLERANCE = "g6"
M5_CLEARANCE_MM = 5.5          # standard close-fit clearance hole for M5
M5_HOLE_COUNT = 4
SIDE_LOAD_N = 500.0
MATERIAL = "aluminum"

REQUEST_VERBATIM = (
    "Design a bearing mount bracket. It carries a 20 mm shaft on a g6 sliding "
    "fit, so the bore must be sized for a clearance fit on a 20 g6 shaft. "
    "The base has 4x M5 clearance holes for bolting down. Material is "
    "aluminium and it will be machined. It has to survive a 500 N side load "
    "on the bore with a safety factor above 1."
)


def mission_prompt(doc_id: str) -> str:
    """The verbatim request, bound to a pre-created document.

    Both loops get the SAME text and the SAME target doc, so a scoring
    difference is a loop difference and not a difference in what was asked.
    """
    return (
        f"{REQUEST_VERBATIM}\n\n"
        f"Build it into the already-open document doc_id={doc_id!r}. "
        f"Do not create a new part — add features to that document."
    )


def build_manifest() -> dict[str, Any]:
    """An ``aicad.deliverables/1`` manifest for the advisory (DAG) loop."""
    return {
        "schema": "aicad.deliverables/1",
        "request_verbatim": REQUEST_VERBATIM,
        "deliverables": [
            {
                "id": "DL1",
                "kind": "part",
                "title": "Bearing mount bracket",
                "depends_on": [],
                "acceptance_criteria": [
                    {
                        "id": "AC1",
                        "text": (
                            f"The bore is sized for a clearance (sliding) fit "
                            f"on a {SHAFT_NOMINAL_MM:g} mm "
                            f"{SHAFT_TOLERANCE} shaft."),
                        "check": {
                            "tool": "fit_report",
                            "pass_rule": "fit_character == 'clearance'",
                        },
                    },
                    {
                        "id": "AC2",
                        "text": (f"The base carries {M5_HOLE_COUNT} M5 "
                                 f"clearance holes."),
                        "check": {
                            "tool": "query_geometry",
                            "pass_rule": (
                                f"{M5_HOLE_COUNT} bores of ~"
                                f"{M5_CLEARANCE_MM:g} mm diameter exist"),
                        },
                    },
                    {
                        "id": "AC3",
                        "text": (f"The bracket survives a {SIDE_LOAD_N:g} N "
                                 f"side load with min FoS > 1."),
                        "check": {
                            "tool": "run_fea_static",
                            "pass_rule": "min_fos > 1.0",
                        },
                    },
                ],
            },
        ],
        "assumptions": [],
        "ambiguities": [],
    }


# ── Instrumentation ──────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    """One observed tool call, enough to reproduce it offline."""

    tool: str
    ok: bool
    args_preview: str = ""
    error_preview: str = ""
    latency_ms: float = 0.0
    actor: str = ""


@dataclass
class LegResult:
    """One (loop, tier, seed) cell."""

    loop: str
    tier: str
    model: str
    seed: int
    run_id: str
    doc_id: str = ""
    ok: bool = False
    stop_reason: str = ""
    error: str = ""
    elapsed_s: float = 0.0
    iterations: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if not c.ok]

    @property
    def failure_rate(self) -> float:
        return len(self.failures) / len(self.tool_calls) if self.tool_calls else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failure_rate"] = self.failure_rate
        return d


class EventSink:
    """Collects the per-iteration signal both loops emit.

    ``agent.tool_call`` is emitted by BOTH loops (it lives in ``agent_turn``),
    so tool-level accounting is directly comparable. ``dag.node.done`` carries
    the T-W5 foreman's ``advisory`` block and only exists on the advisory leg —
    that asymmetry is the thing under test, not a measurement artifact.
    """

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self.advisories: list[str] = []
        self.raw: list[dict] = []

    def __call__(self, kind: str, payload: Any = None) -> None:
        p = payload if isinstance(payload, dict) else {}
        self.raw.append({"kind": kind, **p})
        if kind == "agent.tool_call":
            self.calls.append(ToolCall(
                tool=str(p.get("tool_name") or "?"),
                ok=bool(p.get("ok")),
                args_preview=str(p.get("args_preview") or "")[:600],
                error_preview=str(p.get("error_preview") or "")[:600],
                latency_ms=float(p.get("latency_ms") or 0.0),
                actor=str(p.get("actor") or ""),
            ))
        elif kind == "dag.node.done":
            out = p.get("output")
            if isinstance(out, dict) and out.get("advisory"):
                self.advisories.append(str(out["advisory"])[:1200])


# ── Deterministic scoring ────────────────────────────────────────────────────
#
# Every scorer is a real query against the produced geometry. A scorer that
# cannot run records `None` and says why — it never scores 0.0 for "unknown",
# because a missing measurement and a failed measurement are different facts.


def _dispatch(tool: str, args: dict) -> Any:
    from services.agent import agent_turn as at

    out = at._dispatch_tool(tool, args)
    if isinstance(out, str):
        try:
            return json.loads(out)
        except ValueError:
            return {"__text__": out}
    return out


def _bores(doc_id: str) -> list[dict]:
    """Every concave (bore) face on the doc, with radius and centroid."""
    res = _dispatch("query_geometry", {"doc_id": doc_id, "query": {"of": "face"}})
    matches = res.get("matches") if isinstance(res, dict) else None
    if not isinstance(matches, list):
        return []
    return [m for m in matches
            if isinstance(m, dict) and m.get("concavity") == "bore"
            and isinstance(m.get("radius"), (int, float))]


def score_bore_fit(doc_id: str) -> dict:
    """Is there a bore sized for a clearance fit on a 20 g6 shaft?"""
    bores = _bores(doc_id)
    if not bores:
        return {"passed": False, "reason": "no bore faces found", "measured": None}
    # The shaft bore is the one nearest 20 mm nominal, not merely the largest —
    # a big cosmetic pocket must not be mistaken for the bearing seat.
    best = min(bores, key=lambda m: abs(float(m["radius"]) * 2.0 - SHAFT_NOMINAL_MM))
    measured = float(best["radius"]) * 2.0
    calc = _dispatch("fit_calc", {
        "hole_diameter": measured,
        "shaft_diameter": SHAFT_NOMINAL_MM,
        "shaft_tolerance": SHAFT_TOLERANCE,
    })
    answer = calc.get("answer") if isinstance(calc, dict) else None
    character = (answer or {}).get("fit_character") if isinstance(answer, dict) else None
    return {
        "passed": character == "clearance",
        "measured_bore_mm": round(measured, 4),
        "fit_character": character,
        "clearance": (answer or {}).get("clearance") if isinstance(answer, dict) else None,
    }


def score_hole_pattern(doc_id: str) -> dict:
    """Are there 4 M5-clearance-sized bores (excluding the shaft bore)?"""
    bores = _bores(doc_id)
    tol = 0.9  # mm of slack on the nominal clearance diameter
    m5 = [m for m in bores
          if abs(float(m["radius"]) * 2.0 - M5_CLEARANCE_MM) <= tol]
    return {
        "passed": len(m5) == M5_HOLE_COUNT,
        "found": len(m5),
        "expected": M5_HOLE_COUNT,
        "diameters_mm": sorted(round(float(m["radius"]) * 2.0, 3) for m in m5),
        "all_bore_diameters_mm": sorted(
            round(float(m["radius"]) * 2.0, 3) for m in bores),
    }


def score_manifold(doc_id: str) -> dict:
    from services.verify import battery

    try:
        res = battery.verify(doc_id, ["is_manifold"])
    except Exception as exc:  # noqa: BLE001
        return {"passed": None, "reason": f"{type(exc).__name__}: {exc}"}
    score = (res.get("scores") or {}).get("is_manifold")
    return {"passed": (score == 1.0) if score is not None else None, "score": score}


def score_fea(doc_id: str) -> dict:
    """Min factor of safety under the 500 N side load.

    ``"side"`` is NOT selector vocabulary — the grammar takes
    top/bottom/left/right/front/back (or +x/-x/…). Naming a face the grammar
    cannot parse makes every leg report ``passed: None`` and silently grades
    the whole matrix out of 3 instead of 4.
    """
    try:
        res = _dispatch("run_fea_static", {
            "doc_id": doc_id,
            "fixtures": ["bottom"],
            "loads": [{"face": "right", "force": [SIDE_LOAD_N, 0, 0]}],
            "material": MATERIAL,
        })
    except Exception as exc:  # noqa: BLE001
        return {"passed": None, "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(res, dict) or res.get("ok") is False:
        return {"passed": None,
                "reason": str((res or {}).get("error"))[:240] if isinstance(res, dict)
                else "no result"}
    # The scalars are nested under "summary" — the same shape that made
    # verify_battery grade every solve 1.0. Read there, tolerate flat.
    summary = res.get("summary") if isinstance(res.get("summary"), dict) else {}
    fos = summary.get("min_fos", res.get("min_fos"))
    return {
        "passed": (float(fos) > 1.0) if isinstance(fos, (int, float)) else None,
        "min_fos": fos,
        "max_von_mises_mpa": summary.get("max_von_mises_mpa",
                                         res.get("max_von_mises_mpa")),
        "max_disp_mm": summary.get("max_disp_mm", res.get("max_disp_mm")),
    }


def score_doc(doc_id: str) -> dict:
    """Every deterministic scorer against ONE document, plus a roll-up."""
    scores = {
        "bore_fit": score_bore_fit(doc_id),
        "hole_pattern": score_hole_pattern(doc_id),
        "manifold": score_manifold(doc_id),
        "fea": score_fea(doc_id),
    }
    graded = [v.get("passed") for v in scores.values() if v.get("passed") is not None]
    scores["passed_count"] = sum(1 for g in graded if g)
    scores["graded_count"] = len(graded)
    scores["fraction"] = (scores["passed_count"] / len(graded)) if graded else 0.0
    scores["doc_id"] = doc_id
    return scores


def score_mission(preferred_doc: str) -> dict:
    """Score the document the mission actually BUILT.

    The bound document is only a hint. The advisory loop's BUILD node calls
    `new_part` and works in a document of its own, so grading the bound doc
    would report 0.00 for every advisory leg — a bench artifact, not a result.
    The question worth answering is "did this loop produce a conforming part",
    so score every candidate document in the session and keep the best, and
    record WHICH document was graded so the number stays auditable.
    """
    candidates: list[str] = [preferred_doc] if preferred_doc else []
    listing = _dispatch("list_parts", {})
    for row in (listing or {}).get("docs") or []:
        did = row.get("doc_id") if isinstance(row, dict) else None
        # An empty document cannot satisfy any criterion; skip it so a stray
        # scratch doc never outranks the real one on a tie.
        if did and did not in candidates and not row.get("empty"):
            candidates.append(did)
    if not candidates:
        return {"fraction": 0.0, "graded_count": 0, "passed_count": 0,
                "reason": "no documents open at scoring time", "doc_id": ""}

    scored = []
    for did in candidates:
        try:
            scored.append(score_doc(did))
        except Exception as exc:  # noqa: BLE001
            scored.append({"fraction": 0.0, "doc_id": did,
                           "error": f"{type(exc).__name__}: {exc}"})
    best = max(scored, key=lambda s: (s.get("fraction") or 0.0,
                                      s.get("graded_count") or 0))
    best["candidates_considered"] = [s.get("doc_id") for s in scored]
    best["scored_bound_doc"] = best.get("doc_id") == preferred_doc
    return best


# ── Gap classification ───────────────────────────────────────────────────────
#
# The taxonomy from skill://chudware-harness-live-failure-rate-sweep. Only the
# first three buckets are work; `teaching` is the harness doing its job.

_HARNESS_PATTERNS = (
    (re.compile(r"object has no attribute|Traceback|KeyError|IndexError|"
                r"TypeError: .*positional|NoneType"), "raw python error reached the model"),
    (re.compile(r"No such file or directory|Errno \d+"), "unhandled filesystem error"),
    (re.compile(r"INTERNAL|internal error", re.I), "internal error surfaced"),
)

_DIALECT_PATTERNS = (
    (re.compile(r"unknown argument|no parameter of that name|unexpected keyword"),
     "argument spelling not reconciled"),
    (re.compile(r"unknown op\b|is not a tool|not a valid tool|unknown tool"),
     "op/tool name not mapped"),
)

_GUIDANCE_PATTERNS = (
    (re.compile(r"not enabled in this turn"), "tool needed but not enabled"),
    (re.compile(r"LOOP GUARD"), "model repeated an identical failing call"),
    (re.compile(r"required for|is required\b|must be a non-empty"),
     "required parameter omitted"),
)


def classify_failure(call: ToolCall) -> tuple[str, str]:
    """Bucket one failed call. Returns ``(bucket, why)``."""
    err = call.error_preview or ""
    for rx, why in _HARNESS_PATTERNS:
        if rx.search(err):
            return "harness_defect", why
    for rx, why in _DIALECT_PATTERNS:
        if rx.search(err):
            return "dialect_gap", why
    for rx, why in _GUIDANCE_PATTERNS:
        if rx.search(err):
            return "guidance_gap", why
    return "teaching", "domain/geometry teaching the model should absorb"


def gap_ledger(legs: list[LegResult]) -> list[dict]:
    """Rank every observed failure class across all legs, with a repro."""
    agg: dict[tuple, dict] = {}
    for leg in legs:
        for call in leg.failures:
            bucket, why = classify_failure(call)
            norm = re.sub(r"\d+(\.\d+)?", "<n>", call.error_preview)[:180]
            key = (call.tool, norm)
            row = agg.setdefault(key, {
                "tool": call.tool,
                "bucket": bucket,
                "why": why,
                "count": 0,
                "legs": set(),
                "args_preview": call.args_preview,
                "error_preview": call.error_preview,
            })
            row["count"] += 1
            row["legs"].add(f"{leg.loop}/{leg.tier}")
    out = []
    for row in agg.values():
        row["legs"] = sorted(row["legs"])
        out.append(row)
    # Actionable buckets first, then by frequency.
    order = {"harness_defect": 0, "dialect_gap": 1, "guidance_gap": 2, "teaching": 3}
    out.sort(key=lambda r: (order.get(r["bucket"], 9), -r["count"]))
    return out


# ── Leg execution ────────────────────────────────────────────────────────────


def _new_doc(name: str) -> str:
    res = _dispatch("new_part", {"name": name})
    doc = res.get("doc_id") if isinstance(res, dict) else None
    if not doc:
        raise RuntimeError(f"new_part failed: {str(res)[:300]}")
    return str(doc)


def run_leg(
    *,
    loop: str,
    tier: str,
    provider: Any,
    seed: int,
    max_tool_calls: int = 24,
) -> LegResult:
    """Execute one cell of the matrix and score it."""
    from services.agent import mission_dispatch as md
    from services.agent.providers import token_usage as tu

    model = str(getattr(provider, "model", "") or "")
    run_id = f"tw11-{loop}-{re.sub(r'[^a-z0-9]+', '', tier.lower())}-s{seed}-{uuid.uuid4().hex[:6]}"
    leg = LegResult(loop=loop, tier=tier, model=model, seed=seed, run_id=run_id)
    sink = EventSink()
    t0 = time.time()

    try:
        leg.doc_id = _new_doc(f"tw11_{loop}_s{seed}")
        tu.begin_turn_usage()
        if loop == "baseline":
            res = md.run_linear_role_turn(
                mission_prompt(leg.doc_id),
                "general_agent",
                provider,
                run_id=run_id,
                on_event=sink,
                max_tool_calls=max_tool_calls,
            )
            leg.ok = bool((res or {}).get("success"))
            leg.stop_reason = str((res or {}).get("stop_reason") or "")
        elif loop == "advisory":
            from services.dag import executor, mission_template, node_runner

            graph = mission_template.build_mission_graph(
                build_manifest(), graph_id=f"tw11-{seed}", tier=tier)
            # Bind every buildable node to the pre-created doc so the advisory
            # leg is scored on the SAME document the baseline leg builds into.
            for node in graph.get("nodes") or []:
                node.setdefault("inputs", {})["doc_id"] = leg.doc_id
            agent_fn = node_runner.make_default_agent_fn(provider=provider)
            res = executor.run(graph, run_id=run_id, agent_fn=agent_fn, emit=sink)
            leg.ok = str((res or {}).get("status") or "") in ("done", "completed")
            leg.stop_reason = str((res or {}).get("status") or "")
        else:
            raise ValueError(f"unknown loop {loop!r}")
    except Exception as exc:  # noqa: BLE001 — a leg failure is data, not a crash
        leg.error = f"{type(exc).__name__}: {exc}"
        leg.stop_reason = leg.stop_reason or "exception"
        traceback.print_exc()
    finally:
        leg.elapsed_s = round(time.time() - t0, 2)
        leg.tool_calls = sink.calls
        leg.advisories = sink.advisories
        leg.iterations = len(sink.calls)
        try:
            leg.usage = tu.get_turn_usage() or {}
        except Exception:  # noqa: BLE001
            leg.usage = {}

    try:
        leg.scores = score_mission(leg.doc_id)
    except Exception as exc:  # noqa: BLE001
        leg.scores = {"error": f"{type(exc).__name__}: {exc}", "fraction": 0.0}
    return leg


# ── Reporting ────────────────────────────────────────────────────────────────


def render_markdown(legs: list[LegResult], ledger: list[dict],
                    spend: str = "") -> str:
    lines: list[str] = []
    lines.append("# T-W11 — advisory loop end-to-end benchmark\n")
    lines.append(f"Mission: {REQUEST_VERBATIM}\n")
    lines.append("## Comparison (deterministic scoring only)\n")
    lines.append("| loop | tier | model | seed | ok | stop | iters | "
                 "tool fails | score | tokens | wall s |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for l in legs:
        frac = l.scores.get("fraction")
        frac_s = "n/a" if frac is None else f"{frac:.2f}"
        toks = l.usage.get("total_tokens")
        lines.append(
            f"| {l.loop} | {l.tier} | {l.model} | {l.seed} | "
            f"{'yes' if l.ok else 'NO'} | {l.stop_reason or '-'} | {l.iterations} | "
            f"{len(l.failures)}/{len(l.tool_calls)} | {frac_s} | "
            f"{toks if toks is not None else '-'} | {l.elapsed_s} |")

    lines.append("\n## Per-criterion detail\n")
    lines.append("| loop | tier | seed | bore fit | hole pattern | manifold | FEA |")
    lines.append("|---|---|---|---|---|---|---|")

    def _mark(d: dict) -> str:
        p = d.get("passed")
        return "n/a" if p is None else ("PASS" if p else "FAIL")

    for l in legs:
        s = l.scores
        if not s or "bore_fit" not in s:
            lines.append(f"| {l.loop} | {l.tier} | {l.seed} | - | - | - | - |")
            continue
        lines.append(
            f"| {l.loop} | {l.tier} | {l.seed} | "
            f"{_mark(s['bore_fit'])} ({s['bore_fit'].get('measured_bore_mm')}) | "
            f"{_mark(s['hole_pattern'])} ({s['hole_pattern'].get('found')}/"
            f"{s['hole_pattern'].get('expected')}) | "
            f"{_mark(s['manifold'])} | "
            f"{_mark(s['fea'])} (FoS {s['fea'].get('min_fos')}) |")

    lines.append("\n## Gap ledger — what to actually fix\n")
    if not ledger:
        lines.append("_No tool failures observed._\n")
    else:
        lines.append("| bucket | tool | n | legs | why | verbatim error |")
        lines.append("|---|---|---|---|---|---|")
        for row in ledger:
            err = row["error_preview"].replace("|", "\\|").replace("\n", " ")[:150]
            lines.append(
                f"| **{row['bucket']}** | `{row['tool']}` | {row['count']} | "
                f"{', '.join(row['legs'])} | {row['why']} | {err} |")
    if spend:
        lines.append("\n## Subscription spend\n")
        lines.append("```\n" + spend + "\n```")
    return "\n".join(lines) + "\n"


# ── Flywheel driver ──────────────────────────────────────────────────────────


def _live_enabled() -> bool:
    return str(os.environ.get("CHUDWARE_BENCH_LIVE") or "").strip() in ("1", "true", "yes")


def _core_ready() -> bool:
    try:
        from services.mcp import core_bridge

        return bool(core_bridge.available())
    except Exception:  # noqa: BLE001
        return False


def run_matrix(
    cells: list[tuple[str, str, int]],
    *,
    provider_for: Callable[[str], Any],
    guard: Optional[Any] = None,
    guard_provider_for: Optional[Callable[[str], str]] = None,
    on_leg: Optional[Callable[[LegResult], None]] = None,
) -> list[LegResult]:
    """Run ``(loop, tier, seed)`` cells, stopping cleanly at a spend cap.

    A cap hit ends the matrix and returns the legs already paid for — partial
    results are honest results; discarding them would waste the quota twice.
    """
    from .. import spend_guard as sg

    legs: list[LegResult] = []
    for loop, tier, seed in cells:
        if guard is not None and guard_provider_for is not None:
            try:
                guard.check(guard_provider_for(tier))
            except sg.SpendCapExceeded as exc:
                print(f"[spend] STOP before {loop}/{tier}/s{seed}: {exc}")
                break
        print(f"[leg] {loop}/{tier}/seed{seed} …", flush=True)
        leg = run_leg(loop=loop, tier=tier, provider=provider_for(tier), seed=seed)
        legs.append(leg)
        print(f"[leg] {loop}/{tier}/seed{seed} -> ok={leg.ok} "
              f"stop={leg.stop_reason} fails={len(leg.failures)}/{len(leg.tool_calls)} "
              f"score={leg.scores.get('fraction')} tokens={leg.usage.get('total_tokens')}",
              flush=True)
        if on_leg is not None:
            on_leg(leg)
    return legs


def _run(smoke: bool, workdir: str) -> BenchResult:
    """Flywheel entry point.

    Offline (the default) this scores the deterministic floor: it proves the
    scorers themselves work against a known-good reference part, and records
    the live comparison rows as ``pending``. With ``CHUDWARE_BENCH_LIVE=1`` the
    live matrix runs and its measured rows replace the pending ones.
    """
    examples: list[ExampleScore] = []
    aggregate: dict[str, Any] = {"live": _live_enabled(), "core": _core_ready()}

    if not _core_ready():
        aggregate["note"] = ("C++ core unavailable — deterministic scorers "
                             "cannot query geometry; nothing was measured.")
        return BenchResult(
            bench_id=BENCH_ID, dataset_id=DATASET_ID, dataset_hash="core-absent",
            split="all", n_examples=0, examples=[],
            baselines=[pending_frontier().row()], aggregate=aggregate,
            residual={"status": "pending", "reason": "core unavailable"})

    if not _live_enabled():
        # Deterministic floor: a reference bracket built by fixed code, not a
        # model, proves the scorers discriminate before any quota is spent.
        doc = _new_doc("tw11_reference")
        _dispatch("add_features", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Base",
                "params": {"length": 80, "width": 50, "height": 12}}}]})
        scores = score_doc(doc)
        examples.append(ExampleScore(
            example_id="reference_box_negative_control",
            score=float(scores.get("fraction") or 0.0),
            passed=False,   # a bare box must NOT satisfy the bracket criteria
            structure="deterministic",
            detail=scores))
        aggregate["reference_scores"] = scores
        aggregate["note"] = ("offline floor only — set CHUDWARE_BENCH_LIVE=1 to "
                             "run the paid matrix")
        return BenchResult(
            bench_id=BENCH_ID, dataset_id=DATASET_ID,
            dataset_hash="bearing-mount-bracket-v1", split="all",
            n_examples=len(examples), examples=examples,
            baselines=[pending_frontier().row()], aggregate=aggregate,
            residual={"status": "pending",
                      "reason": "live legs not enabled (CHUDWARE_BENCH_LIVE)"})

    legs = _live_matrix(smoke=smoke)
    for leg in legs:
        examples.append(ExampleScore(
            example_id=f"{leg.loop}/{leg.tier}/s{leg.seed}",
            score=float(leg.scores.get("fraction") or 0.0),
            passed=bool(leg.scores.get("fraction") == 1.0),
            structure=leg.loop,
            detail=leg.to_dict()))
    ledger = gap_ledger(legs)
    aggregate["gap_ledger"] = ledger
    aggregate["by_loop"] = _by_loop(legs)
    measured = Baseline("frontier", kind="frontier", fn=lambda _e: None,
                        available=True, note="live subscription matrix")
    return BenchResult(
        bench_id=BENCH_ID, dataset_id=DATASET_ID,
        dataset_hash="bearing-mount-bracket-v1", split="all",
        n_examples=len(examples), examples=examples,
        baselines=[measured.row(
            mean_score=(sum(e.score for e in examples) / len(examples))
            if examples else None)],
        aggregate=aggregate,
        residual={"status": "measured", "legs": len(legs)})


def _by_loop(legs: list[LegResult]) -> dict:
    out: dict[str, dict] = {}
    for leg in legs:
        row = out.setdefault(leg.loop, {
            "legs": 0, "score_sum": 0.0, "tool_calls": 0, "tool_failures": 0,
            "tokens": 0, "advisories": 0})
        row["legs"] += 1
        row["score_sum"] += float(leg.scores.get("fraction") or 0.0)
        row["tool_calls"] += len(leg.tool_calls)
        row["tool_failures"] += len(leg.failures)
        row["tokens"] += int(leg.usage.get("total_tokens") or 0)
        row["advisories"] += len(leg.advisories)
    for row in out.values():
        row["mean_score"] = row["score_sum"] / row["legs"] if row["legs"] else 0.0
    return out


def _live_matrix(*, smoke: bool) -> list[LegResult]:
    """Build and run the paid matrix. Only reached under CHUDWARE_BENCH_LIVE=1."""
    from services.agent import config as agent_config

    from .. import spend_guard as sg

    tiers = _tiers_from_env()
    loops = ["baseline", "advisory"]
    seeds = [1] if smoke else [1, 2, 3]
    # SEED-MAJOR on purpose. The spend guard can halt the matrix at any cell, so
    # order it such that whatever completed is still a balanced comparison:
    # each seed contributes a full loop x tier block before the next seed
    # starts. A tier-major order would spend the entire budget on one model and
    # leave the comparison unmakeable.
    cells = [(loop, tier, seed)
             for seed in seeds for tier in tiers for loop in loops]

    # Doc-GC retires "abandoned" documents on a grace timer. A live leg runs for
    # minutes with the target doc idle between tool calls, and a retired doc
    # takes the leg's work with it — the geometry the scorers are about to
    # measure. Measured 2026-08-04: hostd logged
    # "doc-gc: retire doc_save(doc-cpp00000010) failed" against a live leg's own
    # document. Pin GC off for the matrix; this is a measurement harness, and
    # the docs are cleaned up with the process.
    os.environ.setdefault("CHUDWARE_DOC_GC", "0")

    cfg = dict(agent_config.load_config())

    def provider_for(tier: str) -> Any:
        name, _, model = tier.partition("/")
        leg_cfg = dict(cfg)
        if model:
            leg_cfg["model"] = model
        return agent_config.get_provider(name, cfg=leg_cfg)

    def guard_provider_for(tier: str) -> str:
        name = tier.split("/", 1)[0]
        return {"anthropic-sub": "anthropic", "kimi-sub": "kimi-code"}.get(name, name)

    guard = sg.SpendGuard()
    # BENCHES_DIR is an os.path string, not a Path — coerce before joining.
    out_dir = Path(BENCHES_DIR) / BENCH_ID / "results"
    out_dir.mkdir(parents=True, exist_ok=True)

    def _checkpoint(leg: LegResult) -> None:
        # Persist after every leg: a cap hit or a crash must never lose a row
        # that real quota already paid for.
        (out_dir / f"leg_{leg.run_id}.json").write_text(
            json.dumps(leg.to_dict(), indent=2, default=str))

    # A private daemon, not the one behind the user's window. core_bridge
    # attaches to whatever hostd is already listening, so without this the
    # matrix shares an engine with the GUI and with any concurrent pytest run.
    # Measured 2026-08-04: during one live leg the shared daemon's open-document
    # count climbed 2 -> 15 from a parallel suite, and the leg's own target doc
    # was destroyed mid-run. Legs scored against a contended engine measure the
    # contention, not the loop.
    from services.mcp import private_hostd

    with private_hostd.private_daemon() as isolated:
        if not isolated:
            print("[warn] NOT isolated — an explicit CHUDWARE_HOSTD_SOCKET or a "
                  "missing daemon binary means this shares an engine; results "
                  "may reflect interference.", flush=True)
        legs = run_matrix(cells, provider_for=provider_for, guard=guard,
                          guard_provider_for=guard_provider_for,
                          on_leg=_checkpoint)

    report = render_markdown(legs, gap_ledger(legs), spend=guard.report())
    (out_dir / "comparison.md").write_text(report)
    print(f"\n[report] {out_dir / 'comparison.md'}")
    print(guard.report())
    return legs


def _tiers_from_env() -> list[str]:
    """``CHUDWARE_BENCH_TIERS`` as ``provider/model`` pairs, comma-separated."""
    raw = os.environ.get("CHUDWARE_BENCH_TIERS") or (
        "anthropic-sub/claude-opus-5,kimi-sub/k3-256k")
    return [t.strip() for t in raw.split(",") if t.strip()]


DRIVER = BenchDriver(BENCH_ID, _run)
