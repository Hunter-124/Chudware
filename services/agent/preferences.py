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
        ("model", "thinking", "ladder", "delegation_depth", "compaction", "roster_width")),
    "location": Preference("cloud", "This computer only keeps technical data on this PC.",
        ("cloud", "this_computer_only"),
        ("offline_mode", "compliance_mode", "provider_filter", "vision_routing", "private_seat")),
    "double_check": Preference(True, "Measures and reviews before done; verification remains a floor.",
        (True, False), ("advisor", "verify_battery", "interference", "reviewer")),
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
    "chat": {"thinking": "low", "delegation_depth": 0},
    "modelling": {"thinking": "medium", "delegation_depth": 0},
    "analysis": {"thinking": "high", "delegation_depth": 0},
    "mission": {"thinking": "high", "delegation_depth": 2},
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


def _requests_delegation(prompt: str) -> bool:
    """Recognize explicit delegation requests, not geometric parallel constraints."""
    for clause in re.split(r"[.!?;,\n]|\b(?:and|but)\b", prompt.lower()):
        if re.search(r"\b(?:do not|don't|never|without|avoid|no)\b", clause):
            continue
        if (re.search(r"\b(?:orchestrat\w*|paralleliz\w*|parallelis\w*|delegat\w*)\b", clause)
                or re.search(r"\b(?:use|using|spawn|launch|run|dispatch|employ)\b.*\bsub[- ]?agents?\b", clause)
                or re.search(r"\bparallel\s+(?:sub[- ]?agents?|agents?|tasks?|work)\b", clause)
                or re.search(r"\b(?:sub[- ]?agents?|agents?|tasks?|work)\b.*\bin parallel\b|\bin parallel\b.*\b(?:sub[- ]?agents?|agents?|tasks?|work)\b", clause)):
            return True
    return False


@dataclass(frozen=True)
class TurnPreferences:
    preferences: CopilotPreferences
    task_kind: str
    thinking: str
    delegation_depth: int
    task_list: bool
    advisor: bool
    verify_battery: bool
    sim_precheck_block: bool
    schedule_compaction: bool
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

    def _guidance_lines(self, *, enforced_at_dispatch: bool) -> list[str]:
        lines = [f"Work style: {self.preferences.work_style}. Priority: {self.priority}."]
        if self.mode == "plan":
            lines.append("Show a plan first; ask for approval before overwriting or destructive changes.")
        if self.mode == "ask":
            lines.append("Answer and measure only. Never change documents or files.")
        if enforced_at_dispatch and self.mode in ("plan", "ask"):
            lines.append("Read-only turn: mutating tools are refused.")
        if self.task_list:
            lines.append("Publish and maintain a task list for this multi-step job.")
        guidance = ("use a strong planner and cheap independent leaves." if self.cheap_leaves
                    else "delegate as appropriate." if self.delegation_depth > 0
                    else "do this work in this turn.")
        lines.append(f"Delegation depth limit: {self.delegation_depth}; {guidance}")
        if enforced_at_dispatch and not self.advisor:
            lines.append("The advisor is off this turn; advise calls are refused.")
        if self.verify_battery:
            lines.append("Run verify_battery before declaring geometry complete.")
        if self.sim_precheck_block:
            lines.append("Simulation setup prechecks are mandatory; do not solve past a failed precheck.")
        return lines

    def prompt_guidance(self) -> str:
        """Preference guidance for a system prompt (the explicit-offline lane)."""
        return ("SESSION PREFERENCES (override earlier role-card defaults)\n"
                + "\n".join(self._guidance_lines(enforced_at_dispatch=False)))

    def turn_notice(self) -> str:
        """The online lane's per-turn policy, appended to the end of the new user message.

        It never enters the system prompt or a tool definition (prefix-cache
        invariant): every turn restates it, and dispatch refuses what it forbids.
        """
        return ("TURN PREFERENCES (apply to this message; they replace any earlier turn "
                "preferences and override role-card defaults)\n"
                + "\n".join(self._guidance_lines(enforced_at_dispatch=True)))


def resolve_turn_preferences(prompt: str, *, cfg: Mapping[str, Any] | None = None,
                             doc_count: int = 1,
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
    thinking = rule["thinking"]
    if prefs.priority == "best_results":
        thinking = "high"
    elif prefs.priority in ("fastest", "lowest_cost"):
        thinking = "low"
    from services.agent.config import get_max_subagent_depth

    agent_cfg = cfg.get("agent")
    explicit_depth = isinstance(agent_cfg, Mapping) and "max_subagent_depth" in agent_cfg
    if prefs.work_style == "answer_only":
        depth = 0
    elif explicit_depth or _requests_delegation(prompt):
        depth = get_max_subagent_depth(dict(cfg))
    else:
        depth = rule["delegation_depth"]
    crowded = context_window > 0 and context_tokens >= context_window * 0.5
    return TurnPreferences(prefs, kind, thinking, depth,
        kind == "mission" or (kind != "chat" and prefs.work_style == "check_with_me"),
        prefs.double_check and kind != "chat",
        kind != "chat", kind == "analysis", crowded,
        kind == "mission", kind == "mission" and depth > 0)
