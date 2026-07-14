"""ITAR-grade outbound redaction pipeline (WS3 / compliance_mode).

When ``compliance_mode`` is ``"redact"``, cloud-bound provider traffic is wrapped
so that controlled technical data never leaves the machine in cleartext:

1. **Deterministic pass** — GSL-aware scrub of dimensions/units, ISO fits,
   tolerances, material specs, part/drawing numbers, and project identifiers
   into stable placeholders (``DIM_1``, ``MAT_1``, ``PN_1``, …).
2. **Optional local-LLM pass** — when a loopback-only redaction-role model
   resolves, ask it to flag residual sensitive spans. Failures degrade to
   deterministic-only (logged warning); never fall through to sending raw.
3. **Rehydrate** — inbound model responses (and tool-call args that echo
   placeholders) are mapped back via a per-session local table before display
   or tool dispatch.
4. **Images blocked** in redact mode (v1 honest limitation: no image redaction).

``local_only`` mode is enforced at ``config.get_provider`` (reuses offline_mode
machinery) and refuses every non-loopback provider.

The mapping table lives in-process on the wrapper (one session = one wrapper
instance). Placeholders are stable across turns so multi-turn conversations keep
consistent tokens for the same underlying value.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .providers.base import Provider

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- modes
COMPLIANCE_SETTING = "compliance_mode"
COMPLIANCE_OFF = "off"
COMPLIANCE_REDACT = "redact"
COMPLIANCE_LOCAL_ONLY = "local_only"
_VALID_COMPLIANCE = {COMPLIANCE_OFF, COMPLIANCE_REDACT, COMPLIANCE_LOCAL_ONLY}

# Placeholder families → category prefix used in tokens
_CAT_DIM = "DIM"
_CAT_MAT = "MAT"
_CAT_PN = "PN"
_CAT_PROJ = "PROJ"
_CAT_SPAN = "SPAN"  # residual spans from optional LLM pass

# --------------------------------------------------------------------------- materials
# Common aerospace/mechanical alloy & polymer designations. Longer tokens first so
# "6061-T6" wins over bare "6061". Case-insensitive match; original case preserved
# in the mapping table for rehydrate fidelity.
_MATERIAL_SPECS: tuple[str, ...] = (
    # Aluminum alloys + temper
    r"7075-T6", r"7075-T73", r"7075-T7351", r"6061-T6", r"6061-T651", r"6061-T4",
    r"2024-T3", r"2024-T4", r"2024-T351", r"7050-T7451", r"5083-H116", r"5052-H32",
    r"AL\s*7075", r"AL\s*6061", r"AL\s*2024",
    r"Aluminium\s+6061-T6", r"Aluminum\s+6061-T6",
    r"Aluminium\s+6061", r"Aluminum\s+6061",
    r"Aluminium\s+7075", r"Aluminum\s+7075",
    # Steels
    r"AISI\s*4140", r"AISI\s*4340", r"AISI\s*1018", r"AISI\s*1020", r"AISI\s*1045",
    r"SAE\s*4140", r"SAE\s*4340", r"SAE\s*1018",
    r"A36\s+steel", r"A36",
    r"4140\s+steel", r"4340\s+steel", r"1018\s+steel", r"mild\s+steel",
    r"SS\s*316L", r"SS\s*316", r"SS\s*304L", r"SS\s*304",
    r"304\s+stainless", r"316\s+stainless", r"316L\s+stainless",
    r"17-4\s*PH", r"15-5\s*PH",
    # Titanium / nickel / copper alloys
    r"Ti-6Al-4V", r"Ti6Al4V", r"Grade\s*5\s+titanium", r"titanium\s+Grade\s*5",
    r"Inconel\s*718", r"Inconel\s*625", r"Hastelloy\s*C-?276",
    r"brass\s*C360", r"C360\s+brass", r"C36000",
    # Polymers / composites commonly called out as material specs
    r"PEEK", r"ULTEM\s*9085", r"ULTEM\s*1010", r"Delrin", r"POM-C",
    r"carbon\s+fiber", r"GFRP", r"CFRP",
)

# Project / program identifiers often leak controlled context
_PROJECT_ID_PATTERNS: tuple[str, ...] = (
    r"\b(?:ITAR|EAR|CUI|FOUO)[-_\s]?(?:restricted|controlled)?\b",
    r"\b(?:program|project|mission|contract)\s*(?:id|no\.?|number|#)?\s*[:=]?\s*"
    r"[A-Z]{2,6}[-_]?\d{2,8}(?:[-_][A-Z0-9]{1,6})?\b",
    r"\b(?:PRJ|PROG|MSN|CTR)[-_][A-Z0-9]{2,12}\b",
)

# Part / drawing numbers: DWG-1234, P/N ABC-12, drawing 100-200-A, PN: X12345
_PART_NUMBER_PATTERNS: tuple[str, ...] = (
    r"\b(?:P/?N|P/N|PN|part\s*(?:no\.?|number|#)|drawing\s*(?:no\.?|number|#)?|DWG|dwg)"
    r"\s*[:=]?\s*[A-Z0-9][A-Z0-9._/-]{2,24}\b",
    r"\b(?:DWG|PN|P/N)[-_][A-Z0-9][A-Z0-9._/-]{1,20}\b",
    r"\b[A-Z]{1,3}\d{2,4}[-_]\d{2,6}(?:[-_][A-Z0-9]{1,4})?\b",  # AB12-3456-A style
)

# ISO hole/shaft fits & unilateral tolerances: H7, g6, H7/g6, js6, F8, …
# Keep letter+grade tight so we don't eat plain "H7" mid-word.
_ISO_FIT_RE = re.compile(
    r"\b([A-Za-z]{1,2}\d{1,2}(?:\s*/\s*[A-Za-z]{1,2}\d{1,2})?)\b"
)
# Only treat as a fit when the letter part looks like an ISO fit letter class.
_ISO_FIT_LETTER = re.compile(
    r"^(?:[A-Z]{1,2}\d{1,2}(?:\s*/\s*[a-z]{1,2}\d{1,2})?|"
    r"[a-z]{1,2}\d{1,2}(?:\s*/\s*[A-Z]{1,2}\d{1,2})?|"
    r"[A-Z]{1,2}\d{1,2}\s*/\s*[A-Z]{1,2}\d{1,2}|"
    r"[a-z]{1,2}\d{1,2}\s*/\s*[a-z]{1,2}\d{1,2})$"
)
_ISO_FIT_KNOWN = re.compile(
    r"^(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}$|"
    r"^(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}\s*/\s*(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}$"
)
# Prefer known ISO fit letter sets to cut false positives (H,G,F,E,D,C,B,A,JS,CD,… + shafts)
_ISO_FIT_STRICT = re.compile(
    r"\b("
    r"(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2}"
    r"(?:\s*/\s*(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2})?"
    r")\b"
)
_ISO_FIT_LETTERS = {
    "A", "B", "C", "CD", "D", "E", "EF", "F", "FG", "G", "H", "JS", "J", "K", "M",
    "N", "P", "R", "S", "T", "U", "V", "X", "Y", "Z", "ZA", "ZB", "ZC",
}

# Numeric dimensions with units. Order: compound first, then simple.
# Captures: 12.5 mm, 0.25 in, 45 deg, 30°, 1/4", 3/8 in, Ø20 mm, dia 10mm, R3.0
_DIM_UNIT_RE = re.compile(
    r"(?:"
    # diameter prefixes (Ø, ⌀, dia, diam) — radius handled separately (word-bounded)
    r"(?:[Ø⌀]|[Dd]ia(?:m(?:eter)?)?\.?)"
    r"\s*"
    r"(?P<num1>[+-]?\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?)"
    r"\s*(?P<u1>mm|cm|m|in|inch|inches|\"|''|mil|µm|um|μm|deg|°|rad|degrees?)?"
    r"|"
    # radius: R3 / R 3.0 / R3.0mm — whole-word R so "chamfer" is safe
    r"\b[Rr]"
    r"\s*"
    r"(?P<numR>[+-]?\d+(?:[.,]\d+)?)"
    r"\s*(?P<uR>mm|cm|m|in|inch|inches|\"|''|mil|µm|um|μm)?"
    r"\b"
    r"|"
    # plain number + unit (incl. fraction 1/4 in)
    r"(?P<num2>[+-]?\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?)"
    r"\s*(?P<u2>mm|cm|m|in|inch|inches|\"|''|mil|µm|um|μm|deg|°|rad|degrees?)\b"
    r"|"
    # bare degree symbol glued: 45°
    r"(?P<num3>[+-]?\d+(?:[.,]\d+)?)\s*(?P<u3>°)"
    r")"
)

# Bilateral / unilateral tolerance: ±0.02, +/-0.05 mm, +0.02/-0.01
_TOL_RE = re.compile(
    r"(?:"
    r"(?:±|\+/-|\+/?-)\s*(?P<t1>\d+(?:[.,]\d+)?)\s*(?P<tu1>mm|in|mil|µm|um|μm)?"
    r"|"
    r"\+(?P<t2>\d+(?:[.,]\d+)?)\s*/\s*-(?P<t3>\d+(?:[.,]\d+)?)"
    r"\s*(?P<tu2>mm|in|mil|µm|um|μm)?"
    r")"
)

# Placeholder token form: DIM_1, MAT_12, PN_3, PROJ_1, SPAN_4
_PLACEHOLDER_RE = re.compile(r"\b((?:DIM|MAT|PN|PROJ|SPAN)_\d+)\b")

# Image part detection
_IMAGE_TYPES = {"image", "image_url", "input_image"}


# --------------------------------------------------------------------------- mapping table
@dataclass
class RedactionMap:
    """Per-session bidirectional placeholder map. Stable across turns."""

    # original_value -> placeholder  (identity for equal values)
    forward: dict[str, str] = field(default_factory=dict)
    # placeholder -> original_value
    reverse: dict[str, str] = field(default_factory=dict)
    # per-category counters
    _counters: dict[str, int] = field(default_factory=dict)

    def placeholder_for(self, original: str, category: str) -> str:
        """Return existing or newly-allocated placeholder for *original*."""
        if original in self.forward:
            return self.forward[original]
        n = self._counters.get(category, 0) + 1
        self._counters[category] = n
        token = f"{category}_{n}"
        self.forward[original] = token
        self.reverse[token] = original
        return token

    def rehydrate(self, text: str) -> str:
        if not text or not self.reverse:
            return text

        def _sub(m: re.Match[str]) -> str:
            return self.reverse.get(m.group(1), m.group(0))

        return _PLACEHOLDER_RE.sub(_sub, text)


# --------------------------------------------------------------------------- deterministic scrub
def _is_iso_fit(token: str) -> bool:
    token = token.strip()
    if not _ISO_FIT_KNOWN.match(token):
        return False
    # Split possible hole/shaft pair
    parts = [p.strip() for p in token.split("/")]
    for p in parts:
        m = re.match(r"^([A-Za-z]{1,2})(\d{1,2})$", p)
        if not m:
            return False
        letters = m.group(1).upper()
        if letters not in _ISO_FIT_LETTERS:
            return False
        grade = int(m.group(2))
        if grade < 1 or grade > 18:
            return False
    return True


def _compile_material_re() -> re.Pattern[str]:
    # Longest first so multi-token specs win
    ordered = sorted(_MATERIAL_SPECS, key=len, reverse=True)
    return re.compile(r"(?:%s)" % "|".join(f"(?:{p})" for p in ordered), re.IGNORECASE)


def _compile_list(patterns: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?:%s)" % "|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_MATERIAL_RE = _compile_material_re()
_PART_NUMBER_RE = _compile_list(_PART_NUMBER_PATTERNS)
_PROJECT_RE = _compile_list(_PROJECT_ID_PATTERNS)


def scrub_text(text: str, mapping: RedactionMap) -> str:
    """Deterministic GSL-aware scrub. Returns text with placeholders substituted."""
    if not text:
        return text

    # Work on a mutable string via span replacement collected then applied
    # right-to-left so indices stay valid.
    spans: list[tuple[int, int, str, str]] = []  # (start, end, original, category)

    def _collect(regex: re.Pattern[str], category: str, predicate=None) -> None:
        for m in regex.finditer(text):
            orig = m.group(0)
            if predicate is not None and not predicate(orig):
                continue
            spans.append((m.start(), m.end(), orig, category))

    _collect(_MATERIAL_RE, _CAT_MAT)
    _collect(_PART_NUMBER_RE, _CAT_PN)
    _collect(_PROJECT_RE, _CAT_PROJ)
    _collect(_TOL_RE, _CAT_DIM)
    _collect(_DIM_UNIT_RE, _CAT_DIM)
    _collect(_ISO_FIT_STRICT, _CAT_DIM, predicate=_is_iso_fit)

    if not spans:
        return text

    # Drop overlaps: keep earliest-starting, then longer; skip any that overlap kept.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept: list[tuple[int, int, str, str]] = []
    occupied_end = -1
    for s in spans:
        if s[0] < occupied_end:
            continue  # overlap with a previously kept span
        kept.append(s)
        occupied_end = s[1]

    # Apply right-to-left
    kept.sort(key=lambda s: s[0], reverse=True)
    out = text
    for start, end, orig, category in kept:
        token = mapping.placeholder_for(orig, category)
        out = out[:start] + token + out[end:]
    return out


def rehydrate_text(text: str, mapping: RedactionMap) -> str:
    """Map placeholders back to originals."""
    return mapping.rehydrate(text)


# --------------------------------------------------------------------------- structured message scrubbing
def _scrub_json_value(value: Any, mapping: RedactionMap) -> Any:
    if isinstance(value, str):
        return scrub_text(value, mapping)
    if isinstance(value, dict):
        return {k: _scrub_json_value(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json_value(v, mapping) for v in value]
    return value


def _rehydrate_json_value(value: Any, mapping: RedactionMap) -> Any:
    if isinstance(value, str):
        return rehydrate_text(value, mapping)
    if isinstance(value, dict):
        return {k: _rehydrate_json_value(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_rehydrate_json_value(v, mapping) for v in value]
    return value


def scrub_messages(
    messages: list[dict],
    mapping: RedactionMap,
    *,
    block_images: bool = True,
) -> tuple[list[dict], int]:
    """Deep-scrub message content (text + tool-call args). Returns (msgs, images_blocked)."""
    images_blocked = 0
    out: list[dict] = []
    for msg in messages:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = scrub_text(content, mapping)
        elif isinstance(content, list):
            new_parts: list = []
            for part in content:
                if not isinstance(part, dict):
                    new_parts.append(part)
                    continue
                p = dict(part)
                ptype = p.get("type", "")
                if ptype in _IMAGE_TYPES or "path" in p and ptype == "image":
                    if block_images:
                        images_blocked += 1
                        # Drop image part entirely; leave a text marker so the model
                        # knows vision was withheld under compliance policy.
                        new_parts.append({
                            "type": "text",
                            "text": "[image redacted — compliance_mode=redact blocks outbound images]",
                        })
                        continue
                if ptype == "text" and "text" in p:
                    p["text"] = scrub_text(str(p["text"]), mapping)
                elif "text" in p and isinstance(p["text"], str):
                    p["text"] = scrub_text(p["text"], mapping)
                # Tool-call shaped parts (OpenAI/Anthropic variants)
                if "input" in p:
                    p["input"] = _scrub_json_value(p["input"], mapping)
                if "arguments" in p:
                    args = p["arguments"]
                    if isinstance(args, str):
                        # JSON string args — scrub then try to keep valid JSON
                        try:
                            parsed = json.loads(args)
                            p["arguments"] = json.dumps(
                                _scrub_json_value(parsed, mapping), ensure_ascii=False
                            )
                        except (json.JSONDecodeError, TypeError):
                            p["arguments"] = scrub_text(args, mapping)
                    else:
                        p["arguments"] = _scrub_json_value(args, mapping)
                if "name" in p and isinstance(p.get("name"), str):
                    # tool names are not redacted
                    pass
                new_parts.append(p)
            m["content"] = new_parts
        # Top-level tool_calls (OpenAI chat format)
        if "tool_calls" in m and isinstance(m["tool_calls"], list):
            tcs = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    tcs.append(tc)
                    continue
                tc2 = dict(tc)
                fn = tc2.get("function")
                if isinstance(fn, dict):
                    fn2 = dict(fn)
                    args = fn2.get("arguments")
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                            fn2["arguments"] = json.dumps(
                                _scrub_json_value(parsed, mapping), ensure_ascii=False
                            )
                        except (json.JSONDecodeError, TypeError):
                            fn2["arguments"] = scrub_text(args, mapping)
                    elif args is not None:
                        fn2["arguments"] = _scrub_json_value(args, mapping)
                    tc2["function"] = fn2
                tcs.append(tc2)
            m["tool_calls"] = tcs
        out.append(m)
    return out, images_blocked


def rehydrate_messages(messages: list[dict], mapping: RedactionMap) -> list[dict]:
    """Deep-rehydrate placeholders in a message list (for local display / tool dispatch)."""
    out: list[dict] = []
    for msg in messages:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = rehydrate_text(content, mapping)
        elif isinstance(content, list):
            new_parts = []
            for part in content:
                if not isinstance(part, dict):
                    new_parts.append(part)
                    continue
                p = dict(part)
                if "text" in p and isinstance(p["text"], str):
                    p["text"] = rehydrate_text(p["text"], mapping)
                if "input" in p:
                    p["input"] = _rehydrate_json_value(p["input"], mapping)
                if "arguments" in p:
                    args = p["arguments"]
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                            p["arguments"] = json.dumps(
                                _rehydrate_json_value(parsed, mapping), ensure_ascii=False
                            )
                        except (json.JSONDecodeError, TypeError):
                            p["arguments"] = rehydrate_text(args, mapping)
                    else:
                        p["arguments"] = _rehydrate_json_value(args, mapping)
                new_parts.append(p)
            m["content"] = new_parts
        if "tool_calls" in m and isinstance(m["tool_calls"], list):
            tcs = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    tcs.append(tc)
                    continue
                tc2 = dict(tc)
                fn = tc2.get("function")
                if isinstance(fn, dict):
                    fn2 = dict(fn)
                    args = fn2.get("arguments")
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                            fn2["arguments"] = json.dumps(
                                _rehydrate_json_value(parsed, mapping), ensure_ascii=False
                            )
                        except (json.JSONDecodeError, TypeError):
                            fn2["arguments"] = rehydrate_text(args, mapping)
                    elif args is not None:
                        fn2["arguments"] = _rehydrate_json_value(args, mapping)
                    tc2["function"] = fn2
                tcs.append(tc2)
            m["tool_calls"] = tcs
        out.append(m)
    return out


# --------------------------------------------------------------------------- optional local-LLM residual pass
_LLM_FLAG_SYSTEM = (
    "You are a compliance redaction assistant. Given TEXT already partially redacted "
    "with placeholders like DIM_1/MAT_1/PN_1, list any REMAINING sensitive spans that "
    "look like exact dimensions, tolerances, material grades, part/drawing numbers, "
    "or project identifiers. Reply ONLY with a JSON array of exact substrings to "
    "redact, e.g. [\"secret-bit\", \"12.345\"]. Empty array if none. No prose."
)


def llm_flag_residual(
    text: str,
    mapping: RedactionMap,
    provider: Provider | None,
    *,
    complete_fn: Callable[..., str] | None = None,
) -> str:
    """Optional residual pass. On any failure, returns *text* unchanged (deterministic-only)."""
    if provider is None and complete_fn is None:
        return text
    if not text or not text.strip():
        return text
    try:
        if complete_fn is not None:
            raw = complete_fn(text)
        else:
            assert provider is not None
            raw = provider.complete(
                _LLM_FLAG_SYSTEM,
                [{"role": "user", "content": [{"type": "text", "text": text[:8000]}]}],
                max_tokens=512,
            )
        spans = _parse_span_list(raw)
        out = text
        # Replace longer first to reduce partial-eat issues
        for span in sorted(set(spans), key=len, reverse=True):
            if not span or span not in out:
                continue
            # Skip if it already looks like a placeholder
            if _PLACEHOLDER_RE.fullmatch(span.strip()):
                continue
            token = mapping.placeholder_for(span, _CAT_SPAN)
            out = out.replace(span, token)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "redaction LLM residual pass failed (%s); continuing with deterministic-only",
            exc,
        )
        return text


def _parse_span_list(raw: str) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    # Extract first JSON array if the model wrapped it in prose/fences
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if isinstance(x, (str, int, float)) and str(x).strip()]


# --------------------------------------------------------------------------- redaction-role resolver
def resolve_redaction_provider(cfg: dict | None = None) -> Provider | None:
    """Resolve the redaction-role model. MUST be loopback-only; else return None.

    Prefers WS2 ``role_models.resolve_role_model("redaction")`` (raises
    ``RoleModelError`` on non-loopback — we catch and degrade). Falls back to
    ``cfg['ai_roles']['redaction']`` / ``cfg['redaction']`` and builds a
    loopback provider only. Never constructs a non-loopback provider for this role.
    """
    cfg = dict(cfg or {})
    try:
        from .role_models import RoleModelError, resolve_role_model
        try:
            assignment = resolve_role_model("redaction", cfg)
        except RoleModelError as exc:
            log.warning("redaction role refused: %s", exc)
            return None
        if assignment is None:
            return None
        payload = {
            "provider": getattr(assignment, "provider", None) or "",
            "model": getattr(assignment, "model", None) or "",
        }
        # Prefer base_url if the assignment carries one (future-proof)
        base = getattr(assignment, "base_url", None)
        if base:
            payload["base_url"] = base
        if not payload["provider"]:
            return None
        return _build_loopback_from_assignment(payload, cfg)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("role_models.resolve_role_model failed: %s", exc)

    # Fallback: config.json ai_roles.redaction or redaction block
    roles = cfg.get("ai_roles") or {}
    assignment = roles.get("redaction") or cfg.get("redaction")
    if not assignment or assignment == "auto":
        return None
    if isinstance(assignment, str):
        # "provider:model" shorthand
        if ":" in assignment:
            prov, _, model = assignment.partition(":")
            assignment = {"provider": prov, "model": model}
        else:
            assignment = {"provider": assignment}
    if not isinstance(assignment, dict):
        return None
    return _build_loopback_from_assignment(assignment, cfg)


def _provider_is_loopback(provider: Provider) -> bool:
    if getattr(provider, "offline_safe", False):
        return True
    name = getattr(provider, "name", "")
    if name in ("mock", "mock-roles", "base"):
        return True
    try:
        from . import config as _cfg
        return _cfg.provider_offline_safe(provider)
    except Exception:  # noqa: BLE001
        base = getattr(provider, "base_url", None)
        if not base:
            return False
        from urllib.parse import urlparse
        host = (urlparse(str(base)).hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def _build_loopback_from_assignment(assignment: dict, cfg: dict) -> Provider | None:
    """Construct a provider for the redaction role only if its endpoint is loopback."""
    try:
        from . import config as _cfg
        name = (assignment.get("provider") or "").lower()
        if not name:
            return None
        # Refuse known cloud names up-front (hard rule for redaction role)
        if name in (
            "anthropic", "openai", "gemini", "openrouter", "xai",
            "anthropic-sub", "codex-sub", "cursor-sub", "xai-sub",
            "claude-cli", "codex-cli",
        ):
            # openai with explicit loopback base_url is allowed
            if name != "openai":
                log.warning("redaction role refuses cloud provider %r", name)
                return None
        merged = dict(cfg)
        if assignment.get("model"):
            merged["model"] = assignment["model"]
        # Stash provider-specific base_url if given
        if assignment.get("base_url"):
            sub = {"openai": "openai", "llamacpp": "llamacpp", "vllm": "vllm"}.get(name)
            if sub:
                merged[sub] = dict(merged.get(sub) or {})
                merged[sub]["base_url"] = assignment["base_url"]
        # Build via _resolve_provider WITHOUT compliance wrap / without offline refuse
        # on the outer path — we check loopback ourselves.
        p = _cfg._resolve_provider(name, merged, role=None)
        if not _provider_is_loopback(p):
            log.warning(
                "redaction role provider %r is not loopback — refused",
                getattr(p, "name", name),
            )
            return None
        return p
    except Exception as exc:  # noqa: BLE001
        log.warning("redaction role provider construction failed: %s", exc)
        return None


# --------------------------------------------------------------------------- provider wrapper
class RedactingProvider(Provider):
    """Transparent Provider wrapper that scrubs outbound / rehydrates inbound traffic.

    ``supports_vision`` is forced False in redact mode so the agent loop never
    attaches screenshots that we would then have to drop.
    """

    def __init__(
        self,
        inner: Provider,
        *,
        mapping: RedactionMap | None = None,
        redaction_llm: Provider | None = None,
        enable_llm_pass: bool = True,
        block_images: bool = True,
    ) -> None:
        self._inner = inner
        self.mapping = mapping if mapping is not None else RedactionMap()
        self._redaction_llm = redaction_llm
        self._enable_llm_pass = enable_llm_pass
        self._block_images = block_images
        # Last scrubbed system+messages (for demos / diagnostics)
        self.last_scrubbed_system: str = ""
        self.last_scrubbed_messages: list[dict] = []
        self.last_images_blocked: int = 0

    # -- transparent proxy -------------------------------------------------
    @property
    def name(self) -> str:  # type: ignore[override]
        return getattr(self._inner, "name", "base")

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unknown")

    @property
    def supports_vision(self) -> bool:  # type: ignore[override]
        # Forced off under redact mode so callers don't attach images we must drop.
        return False

    @property
    def offline_safe(self) -> bool:
        return getattr(self._inner, "offline_safe", False)

    @property
    def base_url(self) -> str | None:
        return getattr(self._inner, "base_url", None)

    @property
    def inner(self) -> Provider:
        return self._inner

    # -- Provider interface ------------------------------------------------
    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]

    def complete_ex(
        self, system: str, messages: list[dict], max_tokens: int = 8000
    ) -> tuple[str, str]:
        scrubbed_system = scrub_text(system or "", self.mapping)
        scrubbed_msgs, n_blocked = scrub_messages(
            messages, self.mapping, block_images=self._block_images
        )
        self.last_images_blocked = n_blocked

        if self._enable_llm_pass and self._redaction_llm is not None:
            scrubbed_system = llm_flag_residual(
                scrubbed_system, self.mapping, self._redaction_llm
            )
            # Residual pass over concatenated user text only (cheap); per-part would
            # be more precise but the deterministic pass already covers tool args.
            for m in scrubbed_msgs:
                content = m.get("content")
                if isinstance(content, str):
                    m["content"] = llm_flag_residual(
                        content, self.mapping, self._redaction_llm
                    )
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            part["text"] = llm_flag_residual(
                                str(part.get("text") or ""),
                                self.mapping,
                                self._redaction_llm,
                            )

        self.last_scrubbed_system = scrubbed_system
        self.last_scrubbed_messages = scrubbed_msgs

        text, thinking = self._inner.complete_ex(
            scrubbed_system, scrubbed_msgs, max_tokens=max_tokens
        )
        # Rehydrate for local consumers (display + tool-dispatch). The cloud only
        # ever saw placeholders; we restore originals before the agent loop parses
        # tool calls or shows the reply.
        return (
            rehydrate_text(text or "", self.mapping),
            rehydrate_text(thinking or "", self.mapping),
        )


def wrap_provider(
    provider: Provider,
    *,
    cfg: dict | None = None,
    mapping: RedactionMap | None = None,
    redaction_llm: Provider | None | object = ...,  # type: ignore[assignment]
    enable_llm_pass: bool = True,
) -> RedactingProvider:
    """Wrap *provider* for redact-mode. Resolves redaction LLM unless explicitly passed."""
    llm: Provider | None
    if redaction_llm is ...:
        llm = resolve_redaction_provider(cfg) if enable_llm_pass else None
    else:
        llm = redaction_llm  # type: ignore[assignment]
    return RedactingProvider(
        provider,
        mapping=mapping,
        redaction_llm=llm,
        enable_llm_pass=enable_llm_pass and llm is not None,
        block_images=True,
    )


# --------------------------------------------------------------------------- config helpers (also re-exported via config.py)
def normalize_compliance_mode(value: Any) -> str:
    if value is None:
        return COMPLIANCE_OFF
    s = str(value).strip().lower().replace("-", "_")
    if s in ("localonly", "local_only", "local"):
        return COMPLIANCE_LOCAL_ONLY
    if s in _VALID_COMPLIANCE:
        return s
    return COMPLIANCE_OFF


def compliance_mode(cfg: dict | None = None) -> str:
    """Return normalized compliance_mode from *cfg* (default: load_config)."""
    if cfg is None:
        try:
            from . import config as _cfg
            cfg = _cfg.load_config()
        except Exception:  # noqa: BLE001
            cfg = {}
    # local_only is also implied by legacy offline_mode
    mode = normalize_compliance_mode((cfg or {}).get(COMPLIANCE_SETTING))
    if mode == COMPLIANCE_OFF and (cfg or {}).get("offline_mode"):
        # offline_mode remains the hard local-only switch; compliance_mode is additive.
        # Do NOT auto-upgrade offline → redact; offline is stricter (no cloud at all).
        pass
    return mode
