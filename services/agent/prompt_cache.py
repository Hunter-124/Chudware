"""Precomposed system prompts for both agent lanes, and the fresh-chat baseline.

Two lanes, two different prompts — never mirrored into each other:

* **Online (Chudcode, every normal chat).** ``worker_rpc._agent_admin_turn``
  composes the role prompt with ``mission_dispatch.role_prompt`` and hands it to
  Chudcode as ``append_system_prompt``; the CAD tools travel separately as
  native host-tool definitions from ``chudcode_tools.build_host_tools``.
  :func:`online_prompt` runs those same two calls for the main-chat defaults.
  :func:`baseline` (the GUI context meter) and ``prompt_preview`` report it, and
  :func:`warm` composes it ahead of the first message, which also pays the MCP
  registry import the first turn would otherwise wait on. Chudcode adds its own
  system prompt and builtin tools, and may defer host-tool schemas, so every
  online figure here is an ESTIMATE of what CAD contributes, never a claim
  about the exact upstream token count.

* **Offline (explicit local-model fallback).** ``agent_turn.run_turn`` builds
  the fenced-json protocol prompt from :func:`static_segments` — the ONLY place
  that block order is written down (role card, harness protocol, harness/role
  catalog, project files, standing guidance, tools slot, skills catalog) — and
  ``mission_dispatch.linear_role_prompt`` reuses the tools slot of the cached
  :class:`StaticPrompt` (:func:`get`) when it renders the same bytes.
  ``test_prompt_cache.py`` pins byte-equality between this composition and the
  system prompt an offline turn hands its provider.

Invalidation
------------
Two mechanisms, deliberately belt-and-braces:

* ``invalidate()`` is called by the settings/prompt/rule/tool-option write RPCs.
  This is the *fast* path — it makes a Settings change visible immediately.
* :func:`fingerprint` is the *correctness backstop*: a stat signature over every
  input that can change the composed bytes. Any writer we failed to hook still
  cannot serve a stale prompt, because the fingerprint will not match. A cache
  entry whose fingerprint differs is discarded, never used.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services import paths

#: Name of the segment slot AFTER which request-dependent project-memory recall
#: is spliced by ``run_turn``. Named rather than positional so the ordering can
#: be re-arranged here without silently moving the memory block.
MEMORY_SLOT = "role_catalog"

#: Cache schema version. Bump when the meaning of a stored field changes; an
#: entry written by an older version is discarded rather than misread.
CACHE_VERSION = 3

#: Directories whose ``.py`` / ``.md`` / ``.yaml`` contents can change the
#: composed bytes. Walked to build the fingerprint. Kept explicit: a recursive
#: walk of the whole repo would make the check cost more than the compose.
_CODE_DIRS = ("services/agent", "services/mcp")
_CODE_SUFFIXES = (".py", ".md", ".yaml", ".yml")

#: The fingerprint stats a few hundred files. Cheap (single-digit ms) but the
#: live turn path consults the cache on every turn, so revalidate at most this
#: often in-process. A settings write calls invalidate() and does not wait.
_REVALIDATE_AFTER_S = 2.0

_lock = threading.RLock()
_mem: dict[str, "StaticPrompt"] = {}
_online_mem: dict[str, "OnlinePrompt"] = {}
_fp_cache: tuple[float, str] | None = None


# --------------------------------------------------------------------------- model

@dataclass
class Segment:
    """One labelled block of the static system prompt.

    ``label`` is a stable id (used by :data:`MEMORY_SLOT` and by the GUI meter
    tooltip); ``title`` is the human string the tooltip shows.
    """

    label: str
    title: str
    text: str
    #: Separator used when this segment is appended to the accumulated prompt.
    #: ``run_turn`` joins every block with a blank line EXCEPT the bare
    #: "Allowed tools:" fallback, which uses a single newline — reproduced here
    #: so the composed bytes match the live path exactly.
    sep: str = "\n\n"

    @property
    def tokens(self) -> int:
        return _count_tokens(self.text)


@dataclass
class StaticPrompt:
    """The request-independent portion of a turn's system prompt."""

    role: str
    role_system: str
    allowed: list[str]
    tools_text: str
    segments: list[Segment]
    fingerprint: str
    project_root: str = ""
    capability: str = ""
    warnings: list[str] = field(default_factory=list)

    # -- composition ------------------------------------------------------
    def render(self, *, memory_block: str = "") -> str:
        """Assemble the full system prompt, splicing *memory_block* at its slot."""
        return render_segments(self.segments, memory_block=memory_block)

    # -- reporting --------------------------------------------------------
    def breakdown(self) -> list[dict[str, Any]]:
        """Per-segment token report, largest first, for the meter tooltip."""
        rows = [
            {"label": s.label, "title": s.title,
             "tokens": s.tokens, "chars": len(s.text)}
            for s in self.segments if s.text
        ]
        rows.sort(key=lambda r: -int(r["tokens"]))
        return rows

    @property
    def total_tokens(self) -> int:
        return _count_tokens(self.render())

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "role": self.role,
            "role_system": self.role_system,
            "allowed": list(self.allowed),
            "tools_text": self.tools_text,
            # `sep` MUST be persisted: the "Allowed tools:" fallback uses a
            # single newline, so dropping it made a round-tripped entry render
            # a blank line there instead — a silent byte change introduced by
            # the cache itself, caught by test_disk_round_trip_preserves_the_bytes.
            "segments": [{"label": s.label, "title": s.title, "text": s.text,
                          "sep": s.sep}
                         for s in self.segments],
            "fingerprint": self.fingerprint,
            "project_root": self.project_root,
            "capability": self.capability,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, d: dict) -> "StaticPrompt | None":
        if not isinstance(d, dict) or int(d.get("version") or 0) != CACHE_VERSION:
            return None
        try:
            return cls(
                role=str(d["role"]),
                role_system=str(d.get("role_system") or ""),
                allowed=[str(x) for x in (d.get("allowed") or [])],
                tools_text=str(d.get("tools_text") or ""),
                segments=[Segment(str(s["label"]),
                                  str(s.get("title") or s["label"]),
                                  str(s.get("text") or ""),
                                  str(s.get("sep") or "\n\n"))
                          for s in (d.get("segments") or [])
                          if isinstance(s, dict) and s.get("label")],
                fingerprint=str(d.get("fingerprint") or ""),
                project_root=str(d.get("project_root") or ""),
                capability=str(d.get("capability") or ""),
                warnings=[str(x) for x in (d.get("warnings") or [])],
            )
        except Exception:  # noqa: BLE001 — a malformed entry is a cache miss
            return None


# --------------------------------------------------------------------------- helpers

def _count_tokens(text: str) -> int:
    """Repo-wide 4-chars/token heuristic (shared with ``tiers._count_tokens``)."""
    try:
        from services.agent.tiers import _count_tokens as _ct  # noqa: PLC0415
        return _ct(text)
    except Exception:  # noqa: BLE001
        return (len(text) + 3) // 4


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cache_dir() -> Path:
    """On-disk cache location, created on first use."""
    d = paths.ROOT / "prompt_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stat_sig(p: Path, h: Any) -> None:
    try:
        st = p.stat()
    except OSError:
        return
    h.update(str(p).encode("utf-8", "replace"))
    h.update(f"{st.st_mtime_ns}:{st.st_size}".encode())


def fingerprint(*, project_root: str | None = None) -> str:
    """Stat signature over every input that can change the composed bytes.

    Covers: config.json (settings — tool options, families, harness prefs, the
    autolearn toggle), the authored role cards and their user overrides, the
    Rules spec and its context packs, the skills trees, the project folder
    listing, and the ``services/agent`` + ``services/mcp`` source that supplies
    ``_SYSTEM_BASE``, the harness/catalog blocks and every tool schema.

    Throttled: recomputing on every turn would stat a few hundred files for no
    reason, so a result is reused for :data:`_REVALIDATE_AFTER_S`. A settings
    write calls :func:`invalidate`, which clears the throttle immediately.
    """
    global _fp_cache
    now = time.monotonic()
    with _lock:
        if _fp_cache is not None and (now - _fp_cache[0]) < _REVALIDATE_AFTER_S:
            cached = _fp_cache[1]
        else:
            cached = None
    if cached is not None:
        return _mix(cached, project_root)

    h = hashlib.sha256()
    h.update(f"v{CACHE_VERSION}\n".encode())
    _stat_sig(paths.CONFIG, h)
    root = _repo_root()
    for rel in _CODE_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in {"__pycache__", ".pytest_cache"} and not d.startswith(".")
            )
            for fn in sorted(filenames):
                if fn.endswith(_CODE_SUFFIXES):
                    _stat_sig(Path(dirpath) / fn, h)
    # User-editable overrides that live OUTSIDE the repo.
    for sub in ("prompts", "skills", "context_packs", "triggers.yaml"):
        p = paths.ROOT / sub
        if p.is_file():
            _stat_sig(p, h)
        elif p.is_dir():
            try:
                for fn in sorted(os.listdir(p)):
                    _stat_sig(p / fn, h)
            except OSError:
                pass
    sig = h.hexdigest()[:32]
    with _lock:
        _fp_cache = (now, sig)
    return _mix(sig, project_root)


def _mix(sig: str, project_root: str | None) -> str:
    """Fold the project-folder listing signature into *sig*.

    The composed prompt contains ``project_context_block(root)`` — a listing of
    the CAD files in the active project folder — so adding or removing a file
    there genuinely changes the bytes and must invalidate.
    """
    if not project_root:
        return sig
    h = hashlib.sha256(sig.encode())
    h.update(str(project_root).encode("utf-8", "replace"))
    try:
        p = Path(project_root)
        for entry in sorted(os.listdir(p))[:400]:
            h.update(entry.encode("utf-8", "replace"))
            try:
                h.update(str((p / entry).stat().st_mtime_ns).encode())
            except OSError:
                pass
    except OSError:
        pass
    return h.hexdigest()[:32]


def _key(role: str, capability: str, project_root: str) -> str:
    raw = f"{role}|{capability}|{project_root}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


# --------------------------------------------------------------------------- compose

def static_segments(
    *,
    role: str = "general_agent",
    capability: Any = None,
    project_root: str | None = None,
    role_system: str = "",
    tools_text: str = "",
    allowed: list[str] | None = None,
    include_catalog: bool = True,
    include_skills: bool | None = None,
) -> tuple[list[Segment], list[str]]:
    """THE ordering definition for the static system prompt.

    Returns ``(segments, warnings)``. Every block here is request-independent;
    ``run_turn`` splices request-dependent project memory after the segment
    named :data:`MEMORY_SLOT` and appends skill activation / Rules advisories
    afterwards.

    *include_catalog* mirrors ``run_turn``'s once-per-session behaviour: the
    harness + role-catalog blocks are static within a session and are sent on
    its FIRST turn only.

    *include_skills* controls the skills CATALOG block, defaulting to
    *include_catalog*. ``run_turn`` passes ``False`` and emits it itself,
    because there the catalog is interleaved with request-dependent skill
    ACTIVATION and is gated on catalog-due alone (a subagent still gets it).
    Callers that want the whole static prompt — the cache, the meter baseline —
    leave it defaulted.
    """
    segs: list[Segment] = []
    warn: list[str] = []

    def _add(label: str, title: str, text: str, sep: str = "\n\n") -> None:
        """Append a guarded block.

        Text is stored RAW (not ``rstrip``-ed) because the live path rstrips the
        accumulated left side, not the incoming block — so a trailing newline on
        the FINAL block survives there and must survive here too.
        """
        if text and text.strip():
            segs.append(Segment(label, title, text, sep))

    from services.agent import agent_turn as _at  # noqa: PLC0415

    # Role card and harness protocol are always present as the first two
    # segments, verbatim: `run_turn` opens with `(system_prompt or "") + "\n\n"
    # + _SYSTEM_BASE`, so an empty role card still contributes its separator.
    segs.append(Segment("role", "Role card", role_system or ""))
    segs.append(Segment("base", "Harness protocol", _at._SYSTEM_BASE))

    if include_catalog:
        try:
            from services.agent.role_catalog import (  # noqa: PLC0415
                format_role_catalog_block,
                harness_context_block,
            )
            _add("harness", "Harness context", harness_context_block())
            _add(MEMORY_SLOT, "Role catalog", format_role_catalog_block())
        except Exception as exc:  # noqa: BLE001
            warn.append(f"role_catalog: {type(exc).__name__}: {exc}")

    # The memory slot must exist even when the catalog blocks are suppressed,
    # otherwise a non-first turn would silently drop the memory block instead
    # of splicing it. An empty placeholder carries the slot without bytes.
    if not any(s.label == MEMORY_SLOT for s in segs):
        segs.append(Segment(MEMORY_SLOT, "Role catalog", ""))

    try:
        from services.agent.project_context import project_context_block  # noqa: PLC0415
        _add("project", "Project files", project_context_block(project_root))
    except Exception as exc:  # noqa: BLE001
        warn.append(f"project_context: {type(exc).__name__}: {exc}")

    try:
        from services.agent.autolearn import standing_guidance  # noqa: PLC0415
        _add("autolearn", "Standing guidance", standing_guidance())
    except Exception as exc:  # noqa: BLE001
        warn.append(f"autolearn: {type(exc).__name__}: {exc}")

    if tools_text:
        _add("tools", "Tool schemas", tools_text)
    elif allowed:
        # Single newline, not a blank line: matches run_turn's
        # `system += "\nAllowed tools: " + ...` fallback byte for byte.
        _add("tools", "Tool schemas",
             "Allowed tools: " + ", ".join(sorted(allowed)), "\n")

    want_skills = include_catalog if include_skills is None else include_skills
    if want_skills:
        try:
            from services.agent import skills as _skills  # noqa: PLC0415
            _add("skills", "Skills catalog", _skills.catalog_block())
        except Exception as exc:  # noqa: BLE001
            warn.append(f"skills: {type(exc).__name__}: {exc}")

    return segs, warn


def render_segments(segments: list[Segment], *, memory_block: str = "") -> str:
    """Join *segments* into a system prompt, splicing *memory_block* at its slot.

    The join rules are the live path's, reproduced deliberately rather than
    tidied, because ``run_turn`` calls this and the composed bytes must not
    change: the role card and the harness protocol are joined VERBATIM
    (``run_turn`` does not ``rstrip`` there, so a role card ending in a newline
    really does contribute a third blank line), every later block is appended to
    an ``rstrip``-ed left side, and the bare "Allowed tools:" fallback uses a
    single newline via its segment ``sep``.
    """
    out = ""
    for i, seg in enumerate(segments):
        if seg.text:
            if i == 0:
                out = seg.text
            elif seg.label == "base":
                out = out + seg.sep + seg.text
            else:
                out = out.rstrip() + seg.sep + seg.text
        if seg.label == MEMORY_SLOT and memory_block:
            out = (out.rstrip() + "\n\n" + memory_block) if out else memory_block
    return out


def compose(
    *,
    role: str = "general_agent",
    capability: Any = None,
    project_root: str | None = None,
    include_tools: bool = True,
) -> StaticPrompt:
    """Compose the static prompt from scratch (no cache read).

    Runs the same calls the live path runs, in the live path's order: the role
    card resolution + capability assembly + session preferences that
    ``run_linear_role_turn`` performs, then :func:`static_segments`.
    """
    warn: list[str] = []
    from services.agent import agent_turn as _at  # noqa: PLC0415
    from services.agent.roles import load_role  # noqa: PLC0415

    card = load_role(role)
    try:
        from services.agent.mission_dispatch import (  # noqa: PLC0415
            effective_role_system_prompt,
            linear_role_allowed_tools,
        )
        base_role_system = effective_role_system_prompt(card)
        allowed = linear_role_allowed_tools(card)
    except Exception as exc:  # noqa: BLE001
        warn.append(f"effective_role_policy: {type(exc).__name__}: {exc}")
        base_role_system = card.system_prompt
        allowed = list(card.concrete_tools)
    role_system = base_role_system
    cap_letter = ""
    try:
        from services.agent.prompt_tiers import (  # noqa: PLC0415
            assemble_system_for_capability,
            letter_from_capability,
        )
        cap_letter = str(letter_from_capability(capability) or "")
        role_system = assemble_system_for_capability(
            base_role_system, cap_letter)
    except Exception as exc:  # noqa: BLE001
        warn.append(f"prompt_tiers: {type(exc).__name__}: {exc}")
        role_system = base_role_system

    # Session preferences BEFORE the tools render: a preference that drops a
    # tool must also drop its schema, or the model is shown a tool every call
    # to which will be refused.
    try:
        from services.agent.mission_dispatch import (  # noqa: PLC0415
            _apply_session_preferences,
        )
        allowed, role_system = _apply_session_preferences(role, allowed, role_system)
    except Exception as exc:  # noqa: BLE001
        warn.append(f"session_preferences: {type(exc).__name__}: {exc}")

    tools_text = ""
    if include_tools:
        try:
            from services.agent.tool_schemas import render_tools_block  # noqa: PLC0415
            tools_text, _meta = render_tools_block(
                allowed, resolver=_at.resolver_for(None))
        except Exception as exc:  # noqa: BLE001
            warn.append(f"render_tools_block: {type(exc).__name__}: {exc}")
            tools_text = ""

    segs, seg_warn = static_segments(
        role=role, capability=capability, project_root=project_root,
        role_system=role_system, tools_text=tools_text, allowed=allowed,
    )
    return StaticPrompt(
        role=role,
        role_system=role_system,
        allowed=allowed,
        tools_text=tools_text,
        segments=segs,
        fingerprint=fingerprint(project_root=project_root),
        project_root=str(project_root or ""),
        capability=cap_letter,
        warnings=warn + seg_warn,
    )


# --------------------------------------------------------------------------- cache

def _disk_path(key: str) -> Path:
    return cache_dir() / f"{key}.json"


def _load_disk(key: str, want_fp: str) -> StaticPrompt | None:
    p = _disk_path(key)
    try:
        sp = StaticPrompt.from_json(json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 — missing/corrupt entry is a miss
        return None
    if sp is None or sp.fingerprint != want_fp:
        return None
    return sp


def _store_disk(key: str, sp: StaticPrompt) -> None:
    """Atomic write so a concurrent reader never sees a half-written entry."""
    try:
        p = _disk_path(key)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sp.to_json(), ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 — the cache is an optimisation, never fatal
        pass


def get(
    *,
    role: str = "general_agent",
    capability: Any = None,
    project_root: str | None = None,
    include_tools: bool = True,
    refresh: bool = False,
) -> StaticPrompt:
    """Return the static prompt, composing only when the cache cannot serve it.

    Never raises: a cache failure degrades to a live compose.
    """
    cap = ""
    try:
        from services.agent.prompt_tiers import letter_from_capability  # noqa: PLC0415
        cap = str(letter_from_capability(capability) or "")
    except Exception:  # noqa: BLE001
        cap = ""
    root = str(project_root or "")
    key = _key(role, cap, root)
    want = fingerprint(project_root=project_root)

    if not refresh:
        with _lock:
            hit = _mem.get(key)
        if hit is not None and hit.fingerprint == want:
            return hit
        disk = _load_disk(key, want)
        if disk is not None:
            with _lock:
                _mem[key] = disk
            return disk

    sp = compose(role=role, capability=capability, project_root=project_root,
                 include_tools=include_tools)
    with _lock:
        _mem[key] = sp
    _store_disk(key, sp)
    return sp


# --------------------------------------------------------------------------- online lane

#: Run id of the throwaway ``MissionDispatch`` the online composition binds so
#: the mission-scoped meta tools (``ask_user``, ``delegate`` …) resolve exactly
#: as in a live turn. Nothing is dispatched through it.
_ONLINE_COMPOSE_RUN_ID = "prompt-baseline"


@dataclass
class OnlinePrompt:
    """What CAD hands Chudcode for a fresh main-chat turn, before the request.

    ``system`` is the ``append_system_prompt`` text; ``host_tools`` are the
    host-tool definitions (``name``/``description``/``parameters``) exactly as
    ``chudcode_tools.build_host_tools`` produces them for ``set_custom_tools``.
    """

    role: str
    system: str
    allowed: list[str]
    host_tools: list[dict[str, Any]]
    fingerprint: str
    warnings: list[str] = field(default_factory=list)

    @property
    def host_tools_text(self) -> str:
        return json.dumps(self.host_tools, ensure_ascii=False, separators=(",", ":"))

    @property
    def system_tokens(self) -> int:
        return _count_tokens(self.system)

    @property
    def host_tool_tokens(self) -> int:
        return _count_tokens(self.host_tools_text)

    @property
    def total_tokens(self) -> int:
        return self.system_tokens + self.host_tool_tokens

    def breakdown(self) -> list[dict[str, Any]]:
        """Per-block token estimate, largest first, for the meter tooltip."""
        rows = [
            {"label": "system", "title": "CAD system prompt",
             "tokens": self.system_tokens, "chars": len(self.system)},
            {"label": "host_tools",
             "title": f"CAD host-tool definitions ({len(self.host_tools)})",
             "tokens": self.host_tool_tokens, "chars": len(self.host_tools_text)},
        ]
        rows = [r for r in rows if r["chars"]]
        rows.sort(key=lambda r: -int(r["tokens"]))
        return rows


def compose_online(*, role: str = "general_agent") -> OnlinePrompt:
    """Compose the online main-chat prompt and host tools for Settings defaults.

    The same calls ``worker_rpc._agent_admin_turn`` makes — env tier/capability,
    ``mission_dispatch.role_prompt`` (Settings system-prompt override, mode/scope
    tool policy, preference guidance, MORE TOOLS menu, skills catalog) and
    ``chudcode_tools.build_host_tools`` — with the preferences an empty request
    resolves to under the saved Settings. Per-turn inputs (picked mode, open
    document, enabled categories) are not known here. Raises on failure.
    """
    from services.agent.chudcode_tools import build_host_tools  # noqa: PLC0415
    from services.agent.mission_dispatch import (  # noqa: PLC0415
        MissionDispatch,
        role_prompt,
    )
    from services.agent.preferences import resolve_turn_preferences  # noqa: PLC0415
    from services.agent.roles import load_role  # noqa: PLC0415

    warn: list[str] = []
    want = fingerprint()
    tier = None
    capability = None
    try:
        from services.agent.tiers import detect_tier_from_env  # noqa: PLC0415

        tier = detect_tier_from_env()
        if tier:
            from services.agent.capability import resolve_capability  # noqa: PLC0415

            capability = resolve_capability(tier)
    except Exception as exc:  # noqa: BLE001 — the live turn degrades the same way
        warn.append(f"capability: {type(exc).__name__}: {exc}")

    card = load_role(role)
    preferences = resolve_turn_preferences("")
    system, allowed = role_prompt(
        role, card=card, capability=capability, preferences=preferences
    )
    options = {
        "mode": preferences.mode,
        "advisor": preferences.advisor,
        "max_subagent_depth": preferences.delegation_depth,
    }
    mission_dispatch = MissionDispatch(
        run_id=_ONLINE_COMPOSE_RUN_ID,
        provider=None,
        on_event=None,
        capability=capability,
        tier=tier,
        options=options,
    )
    bindings = build_host_tools({
        "role_card": card,
        "allowed_tools": allowed,
        "mission_dispatch": mission_dispatch,
        "capability": capability,
        "tier": tier,
        "actor": role,
        "run_id": _ONLINE_COMPOSE_RUN_ID,
        "options": options,
        "mode": options["mode"],
        "turn_preferences": preferences,
        "categories": [],
        "enabled_categories": [],
    })
    host_tools = []
    for binding in bindings:
        definition = binding.as_dict()
        host_tools.append({
            "name": definition["name"],
            "description": definition["description"],
            "parameters": definition["input_schema"],
        })
    return OnlinePrompt(
        role=role,
        system=system,
        allowed=list(allowed),
        host_tools=host_tools,
        fingerprint=want,
        warnings=warn,
    )


def online_prompt(*, role: str = "general_agent", refresh: bool = False) -> OnlinePrompt:
    """The online composition, reused in-process while the fingerprint holds."""
    want = fingerprint()
    if not refresh:
        with _lock:
            hit = _online_mem.get(role)
        if hit is not None and hit.fingerprint == want:
            return hit
    op = compose_online(role=role)
    with _lock:
        _online_mem[role] = op
    return op


def invalidate() -> None:
    """Drop every cached entry and the fingerprint throttle.

    Called by the settings / prompt / rule / tool-option write RPCs so a change
    is visible on the next turn instead of up to
    :data:`_REVALIDATE_AFTER_S` later. The fingerprint remains the correctness
    backstop for any writer not hooked here.
    """
    global _fp_cache
    with _lock:
        _mem.clear()
        _online_mem.clear()
        _fp_cache = None
    try:
        for p in cache_dir().glob("*.json"):
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def warm(*, background: bool = True) -> Any:
    """Compose the online main-chat prompt ahead of the user's first message.

    Composition imports the MCP tool registry (~1.9 s in a cold process); doing
    it here keeps that cost off the first turn and makes :func:`baseline`
    answer at once. The offline lane's tools slot is NOT rendered here: it
    composes on the first explicit-offline turn. Returns the thread when
    *background*, else the :class:`OnlinePrompt`.
    """
    if not background:
        return online_prompt()

    def _run() -> None:
        try:
            online_prompt()
        except Exception:  # noqa: BLE001 — warming never breaks the worker
            pass

    t = threading.Thread(target=_run, name="prompt-cache-warm", daemon=True)
    t.start()
    return t


def baseline(*, role: str = "general_agent") -> dict[str, Any]:
    """Estimated CAD contribution to a fresh online chat, for the context meter.

    ``tokens`` estimates the online system prompt plus the host-tool
    definitions CAD hands Chudcode (``estimate: true``, ~4 chars/token).
    Chudcode's own system prompt and builtin tools come on top, and it may
    defer host-tool schemas, so this is not the provider's exact prompt count.
    ``segments`` is the per-block breakdown the tooltip renders.
    """
    try:
        op = online_prompt(role=role)
        return {
            "ok": True,
            "lane": "online",
            "estimate": True,
            "tokens": op.total_tokens,
            "chars": len(op.system) + len(op.host_tools_text),
            "role": op.role,
            "fingerprint": op.fingerprint,
            "allowed_tool_count": len(op.host_tools),
            "segments": op.breakdown(),
            "warnings": op.warnings,
            "note": ("Estimate of what CAD adds to a fresh online chat: its system "
                     "prompt and host-tool definitions. Chudcode's own system "
                     "prompt and builtin tools are not included."),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "lane": "online", "estimate": True, "tokens": 0,
                "chars": 0, "segments": [],
                "error": f"{type(exc).__name__}: {exc}"}
