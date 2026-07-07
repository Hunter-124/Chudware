"""Design-gym challenge suite v1 (T-W27).

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

Determinism: fixtures carry no wall-clock / random inputs; each challenge declares 3
seeds (``seeds``) that select scripted agent-turn variants so the same seed reproduces
the same ledger byte-for-byte.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

# A grader: (ledger_dict, run_context) -> (result: "PASS"|"FAIL", detail: str)
ACGrader = Callable[[dict, dict], "tuple[str, str]"]


@dataclass
class AcceptanceCheck:
    """One ground-truth acceptance criterion the gym grades itself."""
    id: str
    text: str
    grader: ACGrader                  # deterministic; reads the produced ledger


@dataclass
class Challenge:
    id: str                           # C1..C6
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


_CHALLENGE_MODULES = ("c1_flanged_bushing", "c2_clamshell_enclosure",
                      "c3_bearing_block", "c4_two_part_hinge",
                      "c5_bracket_cam", "c6_bushing_mod",
                      "c7_shaft_housing_fit", "c8_motor_bracket_fasteners")


def load_challenges(*, strict: bool = False) -> list[Challenge]:
    """Load every landed challenge module. When *strict*, a missing/broken module
    raises; otherwise it is skipped (mirrors the bench runner's tolerant self-wiring)
    so a partial suite still runs during development."""
    out: list[Challenge] = []
    for name in _CHALLENGE_MODULES:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception:
            if strict:
                raise
            continue
        ch = getattr(mod, "CHALLENGE", None)
        if isinstance(ch, Challenge):
            out.append(ch)
    return out


def load_challenge(cid: str) -> Challenge:
    for ch in load_challenges():
        if ch.id == cid:
            return ch
    raise KeyError(f"unknown challenge {cid!r}")


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
