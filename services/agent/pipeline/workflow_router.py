"""Deterministic task taxonomy and execution structure for mission callers.

Per-turn model and thinking policy belongs to services.agent.preferences.
This classifier remains available to role selection and gym challenge metadata.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

ROUTER_SCHEMA = "aicad.router/1"



# ---------------------------------------------------------------------------
# Task classes
# ---------------------------------------------------------------------------

class TaskClass(str, Enum):
    """Coarse task-class vocabulary for mission execution and role selection.

    Classes correspond to the C-suite challenge categories:
      SIMPLE_PART         single-body, <3 features, no assembly (C1, C6 mod)
      MULTI_FEATURE_PART  single-body, ≥3 features, complex geometry (C1, C3)
      ASSEMBLY            multi-body / hinge / mating parts (C4)
      ENCLOSURE           shell/snap/enclosure + material constraint (C2)
      CAM_PROGRAM         part + machining / toolpaths (C5)
      MODIFICATION        modify an existing part / design change (C6)
      ANALYSIS            FEA / CFD / simulation-primary (C3 FEA leg)
      PROJECT_ADMIN       file-org / cleanup / PLM / rename / archive — NOT a
                          geometry build; routes to a linear organizer turn
      GENERAL_WORK        general/basic-assistant work (files, search, summarize,
                          PLM-lite) — the DEFAULT lane; a free linear general_agent
                          turn, never a multi-step modeling plan
      UNKNOWN             unclassified; general-agent execution fallback
    """
    SIMPLE_PART = "simple_part"
    MULTI_FEATURE_PART = "multi_feature_part"
    ASSEMBLY = "assembly"
    ENCLOSURE = "enclosure"
    CAM_PROGRAM = "cam_program"
    MODIFICATION = "modification"
    ANALYSIS = "analysis"
    PROJECT_ADMIN = "project_admin"
    GENERAL_WORK = "general_work"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Classifier rules (keyword/pattern, priority-ordered)
# ---------------------------------------------------------------------------

# Each rule: (pattern, class, matched_label).  Patterns are case-insensitive re.search
# patterns. First match wins; UNKNOWN is the fallback. matched_label is embedded in the
# RouterResult.reason so the Routing card can show *why* the class was chosen.
_RULES: list[tuple[str, TaskClass, str]] = [
    # Project/file-org admin (highest priority — must win BEFORE the assembly
    # rule below, which otherwise hijacks "rename GA to general assembly" via
    # its bare 'assembl' match). This is NOT a geometry build: file cleanup,
    # PLM registration, renames/moves of files/folders/documents, archiving.
    # 'rename'/'move' alone are too broad (a build turn can legitimately
    # "rename a feature" or "move a hole") so they only fire paired with a
    # file/folder/document/project noun nearby — bare admin verbs (clean up,
    # organize, tidy, deprecate, archive) and PLM/vault/version-control nouns
    # fire on their own since no geometry request phrases them that way.
    (r"\b(clean.?up|organi[sz]e\w*|tidy\w*|deprecat\w*|archiv\w*|folders?\b|"
     r"director(?:y|ies)\b|version\s+control|vault\b|plm\b|"
     r"put(?:\s+\S+){0,3}\s+into\s+plm)\b"
     r"|\bmove\b(?:\s+\S+){0,10}\s+(files?|folders?|documents?|projects?)\b"
     r"|\brename\b(?:\s+\S+){0,10}\s+(files?|folders?|documents?|projects?|parts?)\b",
     TaskClass.PROJECT_ADMIN, "project-admin/file-org terms"),
    # CAM / machining (highest priority — "mill a bracket" is CAM, not SIMPLE_PART)
    (r"\b(cam|toolpath|g.?code|gcode|nc\b|machining|mill|turn|lathe|cutting|zero.?gouge)\b",
     TaskClass.CAM_PROGRAM, "CAM/machining terms"),
    # Assembly / multi-body. assembl\w* is prefix-anchored (\b at start only,
    # like modif\w*/retrofit\w* below) — a bare "assembl" literal never
    # matches "assembly"/"assembled" (the trailing \b fails mid-word), so it
    # was dead weight; \w* lets it actually catch the word family.
    (r"\b(assembl\w*|hinge|mate|joint|two.part|multi.?part|snap.fit|moving part)\b",
     TaskClass.ASSEMBLY, "assembly/mate terms"),
    # Enclosure / shell / housing
    (r"\b(enclosure|housing|shell|clamshell|snap.?lid|lid|cover|box|case)\b",
     TaskClass.ENCLOSURE, "enclosure/housing terms"),
    # Modification of existing part (before geometry rules so "modify … fit a 25mm shaft"
    # stays MODIFICATION rather than falling into multi-feature-part).
    # Note: modif*/retrofit*/redesign* are prefix-anchored (\b at start only) because
    # "modify", "modification", "retrofit" all begin at a word boundary but end mid-word.
    (r"\b(modif\w*|update\b|change\b|adjust\w*|extend\b|retrofit\w*|redesign\w*|make\s+\w+\s+bigger|make\s+\w+\s+smaller)\b",
     TaskClass.MODIFICATION, "modification terms"),
    # Analysis / simulation primary — require analysis intent for bare "stress"/"load"
    # is handled by requiring fea/cfd/simulation/… (not bare structural talk).
    (r"\b(fea|cfd|finite.?element|simulation|thermal|stress|load.?bearing|safety.?factor|margin)\b",
     TaskClass.ANALYSIS, "simulation/analysis terms"),
    # Multi-feature single-body (bolt circle, bore + multiple features)
    (r"\b(bearing.?(block|seat|housing)|bolt.?circle|multiple.?feature|fit.*(bore|shaft|journal)|press.?fit|bushing.*bolt|flange.*bore|clearance.*fit)\b",
     TaskClass.MULTI_FEATURE_PART, "multi-feature geometry terms"),
    # Simple single feature: bracket, block, plate, rib with few constraints
    (r"\b(bracket|mounting|plate|slab|block|rib|gusset|simple|basic)\b",
     TaskClass.SIMPLE_PART, "simple-part terms"),
    # General work / basic-assistant tasks that are NOT a geometry build: file &
    # disk operations, search/list/summarize/read, backups. Positioned AFTER the
    # geometry rules so "open the bracket" still builds; a bare "clean up the
    # files on my disk" / "list my documents" lands here and runs the free linear
    # general_agent lane (no multi-step modeling plan). Anything still unmatched
    # falls to UNKNOWN, which ALSO routes to general_agent.
    (r"\b(files?\b|on\s+(?:my\s+)?disk|search\s+(?:for|the|my)|find\s+(?:the|my|all)|"
     r"list\s+(?:the|my|all)|show\s+me|summar[iy]\w+|explain\b|read\s+the|help\s+me\b|"
     r"sort\s+(?:the|out)|back\s?up)\b",
     TaskClass.GENERAL_WORK, "general-work/file/assistant terms"),
    # In-product capability surfaces (parts library / McMaster / measure / RE / drawing)
    # — classify so the Routing card is honest even when scaffold falls back to tier default.
    (r"\b(mcmaster|parts?\s+library|standard\s+parts?|catalog\s+part|vendor\s+(cad|part)|grabcad|off-?the-?shelf|download\s+(a\s+)?model|import\s+(a\s+)?model)\b",
     TaskClass.UNKNOWN, "parts-library/sourcing capability"),
    (r"\b(reverse\s*-?\s*engineer|from\s+(scan|mesh|stl|step)|rebuild\s+imported)\b",
     TaskClass.UNKNOWN, "reverse-engineering capability"),
    (r"\b(engineering\s+drawing|section\s+view|detail\s+view|titleblock|gd&t|gdt)\b",
     TaskClass.UNKNOWN, "drawing capability"),
    (r"\b(measure|mass\s+propert|interference|fitment|clearance\s+check)\b",
     TaskClass.UNKNOWN, "measure/evaluate capability"),
]


def classify_task(request: str) -> TaskClass:
    """Classify *request* text into a TaskClass.  Deterministic, no model call."""
    return classify_task_detailed(request)[0]


def classify_task_detailed(request: str) -> tuple[TaskClass, str]:
    """Classify *request* and return (TaskClass, matched_label_or_empty)."""
    text = (request or "").lower()
    for pattern, cls, label in _RULES:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            hit = m.group(0)
            return cls, f"{label}: {hit}"
    return TaskClass.UNKNOWN, ""


# Stable display order for padding alternatives when few rules match.
_ALT_PAD_ORDER: tuple[TaskClass, ...] = (
    TaskClass.SIMPLE_PART,
    TaskClass.MULTI_FEATURE_PART,
    TaskClass.ASSEMBLY,
    TaskClass.ENCLOSURE,
    TaskClass.CAM_PROGRAM,
    TaskClass.MODIFICATION,
    TaskClass.ANALYSIS,
    TaskClass.PROJECT_ADMIN,
    TaskClass.GENERAL_WORK,
    TaskClass.UNKNOWN,
)

# Short why-not blurbs when padding non-matched classes for the Routing card.
_ALT_PAD_REASON: dict[TaskClass, str] = {
    TaskClass.SIMPLE_PART: "no simple single-feature geometry cues",
    TaskClass.MULTI_FEATURE_PART: "no multi-feature geometry cues",
    TaskClass.ASSEMBLY: "no assembly/mate cues",
    TaskClass.ENCLOSURE: "no enclosure/housing cues",
    TaskClass.CAM_PROGRAM: "no CAM/machining cues",
    TaskClass.MODIFICATION: "no modification cues",
    TaskClass.ANALYSIS: "no simulation/analysis cues",
    TaskClass.PROJECT_ADMIN: "no file-org/PLM-admin cues",
    TaskClass.GENERAL_WORK: "no general-work/file cues",
    TaskClass.UNKNOWN: "would leave request unclassified",
}


def collect_class_alternatives(
    request: str,
    chosen: TaskClass,
    *,
    min_count: int = 3,
) -> list[dict[str, str]]:
    """Other plausible task classes for transparency (excludes *chosen*).

    Prefer real secondary ``_RULES`` matches on the same request text. When
    fewer than *min_count* alternate matches exist (common for UNKNOWN /
    simple single-hit cases), pad with other vocabulary classes and a short
    why-not reason so the GUI Routing card always has options to expand.
    Deterministic; no model/network.
    """
    text = (request or "").lower()
    seen: set[str] = set()
    alts: list[dict[str, str]] = []

    for pattern, cls, label in _RULES:
        if cls == chosen:
            continue
        key = cls.value
        if key in seen:
            continue
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        seen.add(key)
        hit = m.group(0)
        alts.append({
            "task_class": key,
            "reason": f"also matched {label}: {hit}",
        })

    if len(alts) < min_count:
        for cls in _ALT_PAD_ORDER:
            if cls == chosen:
                continue
            key = cls.value
            if key in seen:
                continue
            seen.add(key)
            alts.append({
                "task_class": key,
                "reason": _ALT_PAD_REASON.get(cls, "not selected"),
            })
            if len(alts) >= min_count:
                break

    return alts




# ---------------------------------------------------------------------------
# Execution structure — which classes skip the build swarm
# ---------------------------------------------------------------------------

# Geometry-BUILD task classes run the deterministic 8-phase build swarm
# (intake -> planner -> modeler -> reviewer -> fixer, compiled as an
# ``aicad.dag/1`` graph by services.dag.mission_template). EVERYTHING ELSE is
# GENERAL WORK: file/PLM/research/basic-assistant asks that must NOT be forced
# into a multi-step modeling plan. They run as ONE free linear agent_turn against
# a role card (services.agent.agent_turn — no dag/swarm compilation) that starts
# with the file/shell/plm floor and can pull modeling/sketch/sim/... tool packs
# on demand via enable_tools. Inverted default (2026-07): general work is the
# landing zone; only positive geometry-build cues opt into the swarm.
_GEOMETRY_BUILD_CLASSES: frozenset[TaskClass] = frozenset({
    TaskClass.SIMPLE_PART,
    TaskClass.MULTI_FEATURE_PART,
    TaskClass.ASSEMBLY,
    TaskClass.ENCLOSURE,
    TaskClass.CAM_PROGRAM,
    TaskClass.MODIFICATION,
    TaskClass.ANALYSIS,
})

# Explicit per-class linear routing (role card to run). Classes not here and not
# in _GEOMETRY_BUILD_CLASSES fall to the general_agent default lane.
_CLASS_STRUCTURE: dict[TaskClass, tuple[str, str | None]] = {
    TaskClass.PROJECT_ADMIN: ("linear", "project_organizer"),
}


def structure_for_class(task_class: TaskClass) -> tuple[str, str | None]:
    """Return ``(structure, role)`` execution routing for *task_class*.

    ``structure`` is ``"dag"`` (the deterministic build swarm; ``role`` is
    ``None`` — the mission's own role assignment applies) or ``"linear"`` (a
    single agent_turn() tool-calling turn against the named *role* card).

    Inverted default: a geometry-build class runs the swarm; every other class
    (GENERAL_WORK, UNKNOWN, capability labels, …) runs the linear
    ``general_agent`` lane so basic work never gets a multi-step modeling plan.
    """
    explicit = _CLASS_STRUCTURE.get(task_class)
    if explicit is not None:
        return explicit
    if task_class in _GEOMETRY_BUILD_CLASSES:
        return ("dag", None)
    return ("linear", "general_agent")


# ---------------------------------------------------------------------------
# Router result
# ---------------------------------------------------------------------------

@dataclass
class RouterResult:
    """Classification and execution structure for one task."""
    task_class: TaskClass
    tier: str
    reason: str
    request_snippet: str = ""
    alternatives: list[dict[str, str]] = field(default_factory=list)
    structure: str = "dag"
    role: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ROUTER_SCHEMA,
            "task_class": self.task_class.value,
            "tier": self.tier,
            "reason": self.reason,
            "request_snippet": self.request_snippet,
            "alternatives": list(self.alternatives),
            "structure": self.structure,
            "role": self.role,
            "diagnostics": dict(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def route(request: str, tier: str) -> RouterResult:
    """Classify a mission request without selecting a second effort policy."""
    cls, class_why = classify_task_detailed(request)
    structure, exec_role = structure_for_class(cls)
    return RouterResult(
        task_class=cls,
        tier=tier,
        reason=class_why or "unclassified request",
        request_snippet=request[:120],
        alternatives=collect_class_alternatives(request, cls),
        structure=structure,
        role=exec_role,
    )
