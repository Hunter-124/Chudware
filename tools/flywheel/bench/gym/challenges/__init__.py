"""Design-gym challenge suite v1 (T-W27) + two-division extension.

Each challenge is a Python module exposing a module-level ``CHALLENGE`` :class:`Challenge`.
A challenge is a deliverable-manifest fixture (contract §16) plus DETERMINISTIC
ground-truth acceptance-criteria graders the gym runs ITSELF against the produced
ledger/artifacts — never trusting the mission pipeline's own PASS floor.

The suite (task spec):
  C1  flanged bushing         — fit bore + bolt circle
  C2  clamshell enclosure     — snap lid, PETG
  C3  bearing block           — 6202 seat + FEA margin under load
  C4  two-part hinge          — assembly fit + motion clearance
  C5  bracket + CAM program   — zero-gouge
  C6  modification on C1       — "fit a 25mm shaft"
  C7  shaft-housing assembly  — all-custom 2-part running fit + retaining groove
  C8  motor bracket + fasteners — custom parts + catalog M3 screws mated + sized
  C9  edit-from-ref            — edit from description / screenshot
  C10 fix-interference         — find and clear planted clash
  C11 datasheet dims           — dimension from datasheet / calc
  C12 undo recovery            — mid-mission checkpoint/undo recovery
  C13 sketch repair            — under-constrained sketch fix
  C14 interface layout         — mating-face / bolt-pattern layout
  C15 ambiguity clarify        — blocking ambiguity / no silent invent

Determinism: fixtures carry no wall-clock / random inputs; each challenge declares 3
seeds (``seeds``) that select scripted agent-turn variants so the same seed reproduces
the same ledger byte-for-byte.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field, replace
from typing import Any, Callable
import re


# A grader: (ledger_dict, run_context) -> (result: "PASS"|"FAIL", detail: str)
ACGrader = Callable[[dict, dict], "tuple[str, str]"]

# Vision-only tools stripped from LOCAL division cells (HARD filter).
VISION_TOOLS: frozenset[str] = frozenset({
    "render_part", "contact_sheet", "inspect_part", "look_around",
})

# Text-eye tools preferred under LOCAL.
TEXT_EYE_TOOLS: frozenset[str] = frozenset({
    "describe_part", "describe_sketch", "describe_scene", "describe_assembly",
    "measure_part", "query_geometry", "sketch_health",
})

# C1–C8 text-solvable calibration subset (WI-1 / WI-2). C2 is dual-division but not
# in the bridge subset (spec lists C1,C3,C6,C7 + C4/C5/C8 when vision-free graded).
_DEFAULT_CALIBRATION_IDS: frozenset[str] = frozenset({
    "C1", "C3", "C4", "C5", "C6", "C7", "C8",
})


@dataclass
class AcceptanceCheck:
    """One ground-truth acceptance criterion the gym grades itself."""
    id: str
    text: str
    grader: ACGrader                  # deterministic; reads the produced ledger


@dataclass
class Challenge:
    id: str                           # C1..C15
    name: str
    lane: str
    request: str                      # the verbatim user request run_mission receives
    manifest: dict                    # the intake_analyst manifest (fixture; §16)
    checks: list[AcceptanceCheck]     # gym's own ground-truth ACs
    seeds: tuple[int, ...] = (1, 2, 3)
    # scripted per-role agent turns keyed by seed → the agent_fn replays these into the
    # ledger. Shape: {seed: {role_or_node: [event_payload,...]}}. See gym/runner.py.
    scripts: dict[int, dict[str, list[dict]]] = field(default_factory=dict)
    # a modification challenge names the base challenge whose output it edits.
    modifies: str | None = None
    notes: str = ""
    # --- two-division fields (WI-1); defaults preserve C1–C8 dual-division behavior ---
    divisions: tuple[str, ...] = ("local", "open")
    calibration: bool = False
    eyes: str = "either"              # "text" | "vision" | "either"
    # GAP-INJECT (WI-6): runner applies each dict after N build tool_calls.
    # Shape: {after_build_calls: int, fault: str, feature?: dict, note?: str}
    inject_faults: list[dict] = field(default_factory=list)

    def grade(self, ledger: dict, ctx: dict) -> dict:
        """Run every ground-truth check → the gym's independent verdict."""
        crit = []
        for c in self.checks:
            try:
                res, detail = c.grader(ledger, ctx)
            except Exception as exc:  # a grader crash is a FAIL, never a silent pass
                res, detail = "FAIL", f"grader raised {type(exc).__name__}: {exc}"
            crit.append({"id": c.id, "text": c.text, "result": res, "detail": detail})
        passed = all(c["result"] == "PASS" for c in crit)
        return {"challenge": self.id, "criteria": crit, "passed": passed}


# Every suite member is required. Discovery failures are harness failures, not skips.
_CHALLENGE_MODULES = (
    "c1_flanged_bushing", "c2_clamshell_enclosure",
    "c3_bearing_block", "c4_two_part_hinge",
    "c5_bracket_cam", "c6_bushing_mod",
    "c7_shaft_housing_fit", "c8_motor_bracket_fasteners",
    "c9_edit_from_ref", "c10_fix_interference", "c11_datasheet_dims",
    "c12_undo_recovery", "c13_sketch_repair", "c14_interface_layout",
    "c15_ambiguity_clarify",
)


def _apply_legacy_defaults(ch: Challenge) -> Challenge:
    """Stamp C1–C8 calibration defaults without requiring each module to re-declare."""
    if ch.id in _DEFAULT_CALIBRATION_IDS and not ch.calibration:
        return replace(ch, calibration=True)
    return ch


def load_challenges(*, strict: bool = True) -> list[Challenge]:
    """Load the complete challenge suite, failing with the broken module named."""
    if not strict:
        raise ValueError("tolerant challenge discovery is unsupported")
    out: list[Challenge] = []
    for name in _CHALLENGE_MODULES:
        module_name = f"{__name__}.{name}"
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            raise ImportError(
                f"failed to import challenge module {module_name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        ch = getattr(mod, "CHALLENGE", None)
        if not isinstance(ch, Challenge):
            raise TypeError(
                f"challenge module {module_name} has no valid CHALLENGE export"
            )
        out.append(_apply_legacy_defaults(ch))
    return out


def load_challenge(cid: str) -> Challenge:
    for ch in load_challenges():
        if ch.id == cid:
            return ch
    raise KeyError(f"unknown challenge {cid!r}")


def tools_for_division(division: str, tools: list[str] | None = None) -> list[str]:
    """Filter a tool list for the division HARD surface (WI-1.4).

    LOCAL never offers vision tools. OPEN is unrestricted.
    """
    tools = list(tools or [])
    if division == "local":
        return [t for t in tools if t not in VISION_TOOLS]
    return tools


def assert_division_eyes_allowed(challenge: Challenge, division: str) -> None:
    """Refuse vision-eyes challenges under the LOCAL division (WI-1.3)."""
    if division == "local" and challenge.eyes == "vision":
        raise ValueError(
            f"challenge {challenge.id} requires eyes='vision' and cannot run "
            f"under division='local'"
        )


# ---------------------------------------------------------------------------
# Shared grader helpers (deterministic, read only the produced ledger)
# ---------------------------------------------------------------------------

def ledger_dimension(ledger: dict, name_substr: str) -> dict | None:
    """Return the latest dimension event payload whose name contains *name_substr*."""
    hit = None
    for e in ledger.get("events", []):
        if e.get("kind") == "dimension":
            p = e.get("payload") or {}
            if name_substr.lower() in str(p.get("name", "")).lower():
                hit = p
    return hit


def ledger_artifact(ledger: dict, kind: str) -> dict | None:
    for e in ledger.get("events", []):
        if e.get("kind") == "artifact" and (e.get("payload") or {}).get("kind") == kind:
            return e.get("payload")
    return None


def ledger_decision_value(ledger: dict, decision_id: str, key: str) -> Any:
    for e in ledger.get("events", []):
        if e.get("kind") == "decision" and (e.get("payload") or {}).get("id") == decision_id:
            return (e.get("payload") or {}).get(key)
    return None


def coerce_number(value: Any) -> float | None:
    """Parse a free-text or numeric dimension/decision value into a float.

    Real local models often write ISO-style strings like ``"Ø20 H7 (+0.021/0)"``
    or ``"20 mm"`` into ledger dimension payloads. Graders must not crash on those
    — extract the first numeric token, or return None when nothing parses.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    # Prefer the first number that looks like a dimension (before unit/fit noise).
    m = re.search(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except (TypeError, ValueError):
        return None


def dimension_fit_class(dim: dict | None) -> str:
    """Best-effort fit class from a dimension payload (structured or free-text)."""
    if not isinstance(dim, dict):
        return ""
    tol = dim.get("tolerance") or {}
    if isinstance(tol, dict):
        fit = str(tol.get("fit_class") or tol.get("class") or "").upper()
        if fit:
            return fit
    # Free-text value may embed the class, e.g. "Ø20 H7 (+0.021/0)".
    blob = " ".join(str(dim.get(k, "")) for k in ("value", "name", "driver", "text"))
    m = re.search(r"\b([HhGgNnKkPpFfMm]\d+)\b", blob)
    return m.group(1).upper() if m else ""
