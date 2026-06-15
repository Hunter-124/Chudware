"""PropertyManager AI assist.

This module powers the host's ``Select with AI`` button. It asks the active provider for a
strict JSON action plan, then validates the plan against the visible PropertyManager schema before
returning it to the host. The worker never mutates the document and never invents topology refs:
it may fill ordinary fields, activate an existing selector well, highlight fields, reveal fields,
or ask a small intent question.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .ask import build_doc_context
from .providers.base import Provider, text_part, user

PM_ASSIST_ROLE = "pm_assist"

_MAX_PROMPT_CHARS = 12000

_SYSTEM = (
    "You are AI-CAD's PropertyManager assistant. Return ONLY strict JSON. "
    "You help fill the currently active feature form using the provided visible field schema. "
    "Allowed top-level keys: answer, patches, activate_well, reveal_fields, highlights, questions. "
    "patches is an array of {key,value}; patch only ordinary fields from the visible schema. "
    "Never invent topology selectors or references. If a selector/reference is needed, set "
    "activate_well to the visible selector well key and explain what the user should pick. "
    "questions may contain at most 3 multiple-choice questions with 2-3 choices each."
)


def _field_map(form_schema: dict, *, visible_only: bool = False) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in form_schema.get("fields", []) or []:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "")
        if not key:
            continue
        if visible_only and not f.get("visible", True):
            continue
        out[key] = f
    return out


def _selector_keys(fields: dict[str, dict]) -> list[str]:
    return [k for k, f in fields.items() if f.get("kind") in ("selector", "feature_well")]


def _empty_selector_keys(fields: dict[str, dict], current: dict) -> list[str]:
    keys: list[str] = []
    cur_fields = (current or {}).get("fields") or {}
    for k in _selector_keys(fields):
        fcur = cur_fields.get(k) if isinstance(cur_fields, dict) else {}
        if not isinstance(fcur, dict) or int(fcur.get("count") or 0) == 0:
            keys.append(k)
    return keys


def _extract_json(reply: str) -> dict:
    text = (reply or "").strip()
    if not text:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _coerce_patch_value(field: dict, value: Any) -> Any | None:
    kind = field.get("kind")
    if kind == "dim":
        if isinstance(value, (int, float, str)):
            return value
        if isinstance(value, dict):
            if "expr" in value and isinstance(value["expr"], str):
                return {"expr": value["expr"]}
            if "value" in value and isinstance(value["value"], (int, float, str)):
                return value["value"]
        return None
    if kind in ("combo", "datum_ref"):
        if not isinstance(value, str):
            return None
        opts = field.get("options") or []
        return value if value in opts else None
    if kind == "check":
        return value if isinstance(value, bool) else None
    if kind == "int":
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None
    # Selector wells and feature wells are never patched by AI.
    return None


def _clean_questions(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for q in raw[:3]:
        if not isinstance(q, dict):
            continue
        prompt = str(q.get("prompt") or q.get("question") or "").strip()
        choices = q.get("choices") or []
        clean_choices = [str(c).strip() for c in choices if str(c).strip()][:3]
        if prompt and 2 <= len(clean_choices) <= 3:
            out.append({"prompt": prompt, "choices": clean_choices})
    return out


def _validate_plan(plan: dict, form_schema: dict, current: dict) -> dict:
    fields = _field_map(form_schema, visible_only=True)
    all_fields = _field_map(form_schema)
    selectors = set(_selector_keys(all_fields))
    out: dict[str, Any] = {
        "answer": str(plan.get("answer") or "").strip(),
        "patches": [],
        "activate_well": "",
        "reveal_fields": [],
        "highlights": [],
        "questions": _clean_questions(plan.get("questions")),
    }

    for p in plan.get("patches") or []:
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or "")
        if key not in fields or key in selectors:
            continue
        value = _coerce_patch_value(fields[key], p.get("value"))
        if value is not None:
            out["patches"].append({"key": key, "value": value})

    act = str(plan.get("activate_well") or "").strip()
    if act in selectors:
        out["activate_well"] = act
    elif not out["patches"] and not out["questions"]:
        empties = _empty_selector_keys(fields, current)
        if empties:
            out["activate_well"] = empties[0]

    for key in plan.get("reveal_fields") or []:
        key = str(key)
        if key in fields:
            out["reveal_fields"].append(key)
    for key in plan.get("highlights") or []:
        key = str(key)
        if key in fields:
            out["highlights"].append(key)

    if not out["answer"]:
        bits: list[str] = []
        if out["patches"]:
            bits.append(f"I can fill {len(out['patches'])} field(s).")
        if out["activate_well"]:
            bits.append(f"Pick a reference for {out['activate_well']}.")
        if out["questions"]:
            bits.append("I need one intent answer first.")
        out["answer"] = " ".join(bits) or "I do not see a safe field change to make yet."
    return out


def _heuristic_plan(form_schema: dict, current: dict, intent: str) -> dict:
    fields = _field_map(form_schema, visible_only=True)
    all_fields = _field_map(form_schema)
    lower = (intent or "").lower()
    patches: list[dict] = []
    activate = ""

    if form_schema.get("type") == "extrude":
        if "up to" in lower and "end" in fields and "end_ref" in all_fields:
            patches.append({"key": "end", "value": "up_to_reference"})
            activate = "end_ref"
        elif "offset from reference" in lower and "end" in fields and "end_ref" in all_fields:
            patches.append({"key": "end", "value": "offset_from_reference"})
            activate = "end_ref"
        elif "from reference" in lower and "start" in fields and "start_ref" in all_fields:
            patches.append({"key": "start", "value": "from_reference"})
            activate = "start_ref"

    if not activate:
        empties = _empty_selector_keys(fields, current)
        activate = empties[0] if empties else ""

    return {
        "answer": "I used the visible form options and will only ask you to pick real references.",
        "patches": patches,
        "activate_well": activate,
        "reveal_fields": [],
        "highlights": [activate] if activate else [],
        "questions": [],
    }


def build_pm_assist_prompt(params: dict, session=None) -> tuple[str, list[dict]]:
    form_schema = params.get("form_schema") or {}
    current = params.get("current") or {}
    intent = str(params.get("intent") or "")
    selected_refs = params.get("selected_refs") or []
    body = {
        "document_context": build_doc_context(session),
        "intent": intent,
        "form_schema": form_schema,
        "current": current,
        "selected_refs": selected_refs,
    }
    prompt = json.dumps(body, ensure_ascii=False)
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = prompt[:_MAX_PROMPT_CHARS] + "\n...[truncated]"
    return f"ROLE: {PM_ASSIST_ROLE}\n{_SYSTEM}", [user([text_part(prompt)])]


def assist_property_manager(params: dict, session=None, provider: Optional[Provider] = None) -> dict:
    """Return validated PM-assist actions. Mutates nothing."""
    form_schema = params.get("form_schema") or {}
    current = params.get("current") or {}
    intent = str(params.get("intent") or "")

    plan: dict = {}
    provider_error = ""
    if provider is None:
        from . import config
        try:
            provider = config.get_provider()
        except Exception as exc:  # noqa: BLE001
            provider = None
            provider_error = str(exc)

    if provider is not None:
        try:
            system, messages = build_pm_assist_prompt(params, session)
            plan = _extract_json(provider.complete(system, messages, max_tokens=900))
        except Exception as exc:  # noqa: BLE001
            provider_error = str(exc)

    if not plan:
        plan = _heuristic_plan(form_schema, current, intent)
        if provider_error:
            plan["answer"] = plan.get("answer", "") + f" Provider fallback: {provider_error}"

    return _validate_plan(plan, form_schema, current)
