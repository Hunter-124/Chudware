"""Copilot preferences and per-turn policy: one config.json owner, no effort dials."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import re


@dataclass(frozen=True)
class Preference:
    default: Any
    help: str
    choices: tuple[Any, ...]
    derives: tuple[str, ...]


PREFERENCES = {
    "work_style": Preference("check_with_me", "Shows a plan first; asks before it overwrites.",
        ("do_it", "check_with_me", "answer_only"),
        ("copilot_mode", "plan_policy", "task_list", "destructive_approval")),
    "priority": Preference("auto", "Picks the model and thinking for each request.",
        ("auto", "best_results", "fastest", "lowest_cost"),
        ("model", "thinking", "ladder", "council", "delegation_depth", "compaction", "roster_width")),
    "location": Preference("cloud", "This computer only keeps technical data on this PC.",
        ("cloud", "this_computer_only"),
        ("offline_mode", "compliance_mode", "provider_filter", "vision_routing", "private_seat")),
    "double_check": Preference(True, "Measures and reviews before done; verification remains a floor.",
        (True, False), ("advisor", "verify_battery", "interference", "reviewer", "council")),
}


@dataclass(frozen=True)
class CopilotPreferences:
    work_style: str = PREFERENCES["work_style"].default
    priority: str = PREFERENCES["priority"].default
    location: str = PREFERENCES["location"].default
    double_check: bool = PREFERENCES["double_check"].default

    @property
    def mode(self) -> str:
        return {"do_it": "agent", "check_with_me": "plan", "answer_only": "ask"}[self.work_style]

    @property
    def local_only(self) -> bool:
        return self.location == "this_computer_only"


def resolve_preferences(cfg: Mapping[str, Any] | None = None, *,
                        headless: bool = False) -> CopilotPreferences:
    if cfg is None:
        from services.agent.config import load_config
        cfg = load_config()
    raw = (cfg or {}).get("copilot") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("config.json copilot must be an object")
    values = {}
    for name, definition in PREFERENCES.items():
        # Execution lanes must not inherit the interactive approval-first style.
        value = "do_it" if headless and name == "work_style" else raw.get(name, definition.default)
        if value not in definition.choices or (name == "double_check" and not isinstance(value, bool)):
            raise ValueError(f"copilot.{name} must be one of {definition.choices}")
        values[name] = value
    # Existing privacy keys remain authoritative until the Settings cutover.
    from services.agent.config import local_only_enabled

    if local_only_enabled(dict(cfg or {})):
        values["location"] = "this_computer_only"
    return CopilotPreferences(**values)


TASK_RULES = {
    "chat": {"min_rank": 1, "thinking": "low", "delegation_depth": 0},
    "modelling": {"min_rank": 3, "thinking": "medium", "delegation_depth": 0},
    "analysis": {"min_rank": 4, "thinking": "high", "delegation_depth": 0},
    "mission": {"min_rank": 4, "thinking": "high", "delegation_depth": 2},
}
TASK_CLASS_KINDS = {
    "simple": "chat", "leaf": "chat",
    "simple_part": "modelling", "modification": "modelling",
    "multi_feature_part": "modelling", "enclosure": "modelling",
    "analysis": "analysis", "cam_program": "analysis",
    "assembly": "mission", "unknown": "modelling",
}



def keyword_task_kind(prompt: str) -> str:
    text = prompt.lower()
    if re.match(r"\s*(what (is|are)|why|how (does|do i)|explain|define)\b", text):
        return "chat"
    if re.search(r"\b(multi[- ]?(deliverable|document|part)|assembly|assemblies|mission|two (parts|documents)|2 (parts|docs))\b", text):
        return "mission"
    if re.search(r"\b(simulat\w*|analys\w*|fea|stress|thermal|buckling|modal|cfd|fatigue)\b", text):
        return "analysis"
    if re.search(r"\b(build|create|model|design|make|add|edit|delete|remove|extrude|fillet|chamfer|sketch|cut|save|import|export|overwrite|continue|proceed|finish)\b", text):
        return "modelling"
    return "chat"


def classify_task(prompt: str, cfg: Mapping[str, Any] | None = None) -> str:
    from services.agent.model_manager import classify_bundled_task
    local = classify_bundled_task(prompt, cfg=cfg)
    return local if local in TASK_RULES else keyword_task_kind(prompt)


@dataclass(frozen=True)
class TurnPreferences:
    preferences: CopilotPreferences
    task_kind: str
    thinking: str
    min_rank: int
    delegation_depth: int
    task_list: bool
    advisor: bool
    council: bool
    verify_battery: bool
    sim_precheck_block: bool
    schedule_compaction: bool
    trim_tool_schemas: bool
    strong_planner: bool
    cheap_leaves: bool

    @property
    def priority(self) -> str:
        return self.preferences.priority

    @property
    def mode(self) -> str:
        return self.preferences.mode

    @property
    def local_only(self) -> bool:
        return self.preferences.local_only

    def prompt_guidance(self) -> str:
        lines = [f"Work style: {self.preferences.work_style}. Priority: {self.priority}."]
        if self.mode == "plan":
            lines.append("Show a plan first; ask for approval before overwriting or destructive changes.")
        if self.mode == "ask":
            lines.append("Answer and measure only. Never change documents or files.")
        if self.task_list:
            lines.append("Publish and maintain a task list for this multi-step job.")
        lines.append(f"Delegation depth limit: {self.delegation_depth}; " +
                     ("use a strong planner and cheap independent leaves." if self.cheap_leaves else "do this work in this turn."))
        if self.verify_battery:
            lines.append("Run verify_battery before declaring geometry complete.")
        if self.council:
            lines.append("Convene a build-strategy council before building this five-or-more-feature mission.")
        if self.sim_precheck_block:
            lines.append("Simulation setup prechecks are mandatory; do not solve past a failed precheck.")
        return "SESSION PREFERENCES (override earlier role-card defaults)\n" + "\n".join(lines)


def resolve_turn_preferences(prompt: str, *, cfg: Mapping[str, Any] | None = None,
                             feature_count: int = 0, doc_count: int = 1,
                             context_tokens: int = 0, context_window: int = 0,
                             task_kind: str | None = None,
                             headless: bool = False) -> TurnPreferences:
    if cfg is None:
        from services.agent.config import load_config

        cfg = load_config()
    prefs = resolve_preferences(cfg, headless=headless)
    kind = task_kind or classify_task(prompt, cfg)
    if doc_count >= 2:
        kind = "mission"
    if kind not in TASK_RULES:
        raise ValueError(f"Unknown task kind: {kind}")
    rule = TASK_RULES[kind]
    rank, thinking = rule["min_rank"], rule["thinking"]
    if feature_count > 40:
        rank = min(5, rank + 1)
    if prefs.priority == "best_results":
        rank, thinking = 5, "high"
    elif prefs.priority in ("fastest", "lowest_cost"):
        thinking = "low"
    depth = rule["delegation_depth"] if prefs.work_style != "answer_only" else 0
    crowded = context_window > 0 and context_tokens >= context_window * 0.5
    return TurnPreferences(prefs, kind, thinking, rank, depth,
        kind == "mission" or (kind != "chat" and prefs.work_style == "check_with_me"),
        prefs.double_check and kind != "chat",
        prefs.double_check and kind == "mission" and feature_count >= 5,
        kind != "chat", kind == "analysis", crowded, crowded,
        kind == "mission", kind == "mission" and depth > 0)
